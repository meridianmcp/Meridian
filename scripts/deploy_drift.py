#!/usr/bin/env python3
"""056b712f — deploy-drift detection.

Compare the git SHA prod is ACTUALLY running (from ``/health``) against the latest
main-branch commit, and fail loudly (exit 1) when prod has drifted BEHIND main. This is
the durable, mechanism-independent backstop for the whole "CI green but prod stale" class
of bug — confirmed 2026-07-06, when the deploy job's ``github.actor`` gate silently
skipped every direct-push deploy and prod ran a ~19h-old image while every workflow
reported success. It does not depend on remembering a process rule; it structurally
detects the drift regardless of cause.

Fail-open by design: a transient /health or GitHub-API hiccup, or an old build that
doesn't yet expose ``git_sha``, reports NO drift (exit 0) so it never pages on noise.
Only a genuine "main is ahead of the SHA prod is running" pages.

Usage:
    python scripts/deploy_drift.py --url https://usemeridian.us --repo meridianmcp/Meridian
Exit code: 0 = prod up to date with main (or undetermined); 1 = prod drifted behind main.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

_UA = "Mozilla/5.0 (Meridian-deploy-drift)"


def _get_json(url: str, headers: dict | None = None, timeout: int = 15) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return -1, {"_error": str(exc)}


def running_sha_from_health(health: object) -> str:
    """Extract the running build's git SHA from a /health payload. '' if absent."""
    if not isinstance(health, dict):
        return ""
    return str(health.get("git_sha") or "").strip()


def assess_drift(
    prod_sha: str, main_head: str, ahead_by: object, compare_status: str
) -> tuple[bool, str]:
    """Pure decision: has prod drifted BEHIND main? Returns ``(drifted, reason)``.

    Fail-open: unknown prod SHA, a prefix match between the two SHAs, an
    ``identical``/``behind`` compare status (prod at or ahead of main), or an
    unavailable ``ahead_by`` all report NO drift. Only ``main ahead of prod`` drifts.
    """
    ps, mh = (prod_sha or "").lower().strip(), (main_head or "").lower().strip()
    if not ps:
        return False, "prod git_sha unavailable — cannot assess (fail-open)"
    if mh and (ps.startswith(mh) or mh.startswith(ps)):
        return False, f"prod is running main HEAD ({ps})"
    if str(compare_status) in ("identical", "behind"):
        return False, f"prod SHA is at or ahead of main (compare={compare_status})"
    try:
        n = int(ahead_by)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, "compare ahead_by unavailable — cannot assess (fail-open)"
    if n > 0:
        return True, f"prod ({ps}) is {n} commit(s) BEHIND main HEAD ({mh})"
    return False, f"no drift (main ahead_by={n})"


def _line(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK' if ok else 'DRIFT'}] {name}" + (f" -- {detail}" if detail else ""))


def run(base_url: str, repo: str) -> int:
    base = base_url.rstrip("/")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    gh_headers = {"Accept": "application/vnd.github+json"}
    if token:
        gh_headers["Authorization"] = f"Bearer {token}"

    print(f"\nMeridian deploy-drift check -- prod={base} repo={repo}\n")

    code, health = _get_json(f"{base}/health")
    prod_sha = running_sha_from_health(health)
    if code != 200 or not prod_sha:
        _line("prod /health exposes git_sha", False,
              f"status={code} sha={prod_sha!r} — fail-open, not paging")
        return 0
    _line("prod running SHA", True, prod_sha)

    mcode, main = _get_json(f"https://api.github.com/repos/{repo}/commits/main", gh_headers)
    main_head = str((main or {}).get("sha") or "")[:12]
    if mcode != 200 or not main_head:
        _line("latest main SHA", False, f"status={mcode} — fail-open, not paging")
        return 0
    _line("latest main SHA", True, main_head)

    ahead_by: object = None
    status = ""
    if not (prod_sha.startswith(main_head) or main_head.startswith(prod_sha)):
        ccode, comp = _get_json(
            f"https://api.github.com/repos/{repo}/compare/{prod_sha}...{main_head}", gh_headers
        )
        if ccode == 200:
            ahead_by = comp.get("ahead_by")
            status = str(comp.get("status") or "")

    drifted, reason = assess_drift(prod_sha, main_head, ahead_by, status)
    _line("prod up to date with main", not drifted, reason)

    print(f"\n{'=' * 56}")
    print(f"  {'DRIFT' if drifted else 'OK'}: {reason}")
    print(f"{'=' * 56}\n")
    return 1 if drifted else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Meridian deploy-drift check")
    parser.add_argument("--url", default="https://usemeridian.us")
    parser.add_argument("--repo", default="meridianmcp/Meridian")
    args = parser.parse_args()
    sys.exit(run(args.url, args.repo))


if __name__ == "__main__":
    main()
