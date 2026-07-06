"""Independent GitHub Actions CI verification for sprint-item completion (b121348e).

``complete_sprint_item``'s ``required_notes`` gate only proves that evidence TEXT
exists — not that it is TRUE. A self-reported "347 tests pass" is not independently
verified; the same self-report-vs-verified-truth gap the rest of this session kept
hitting, at the CI layer.

This module cross-references the ACTUAL GitHub Actions result for the commit named
in the completion notes, so a green claim can be checked against green CI and a
completion FLAGGED when CI is really failing. It never hard-blocks: CI is usually
still running at completion time (you push, then complete), and self-hosted / no-
GitHub setups have nothing to check — so this is an advisory signal, not a gate.

Best-effort + injectable: the HTTP seam is stubbed in tests, and every error or
missing config degrades to ``state="unknown"`` (never raises).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)

# A git SHA that FOLLOWS a commit-context keyword, so a hex UUID / sprint-item id
# (also hex) sitting in the notes is not mistaken for a commit. Matches e.g.
# "committed 62a3a6c", "commit abc1234", "main 6346a18", "sha: deadbeef", "@1234abc".
_SHA_CONTEXT_RE = re.compile(
    r"(?:commit(?:ted)?|sha|main|@)\b[^0-9a-f]{0,4}([0-9a-f]{7,40})\b",
    re.IGNORECASE,
)
# Conclusions that mean the check did NOT pass. 'neutral' and 'skipped' are OK.
_FAIL_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "stale", "startup_failure"}
)


def extract_commit_sha(text: str | None) -> str | None:
    """Return the first commit SHA referenced in ``text`` (after a commit keyword),
    or None. Keyword-anchored so a hex UUID / item-id is not mistaken for a SHA."""
    if not text:
        return None
    m = _SHA_CONTEXT_RE.search(str(text))
    return m.group(1).lower() if m else None


async def verify_commit_ci(
    repo: str,
    sha: str,
    *,
    token: str | None = None,
    http_get: Callable[..., Awaitable[Any]] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Aggregate the GitHub Actions state for ``sha`` in ``repo`` (best-effort).

    Returns ``{sha, repo, state, total, failed}`` where ``state`` is
    ``success | failure | pending | unknown``. Any error, missing repo/sha, or a
    commit with no check-runs yields ``state="unknown"``. ``http_get`` is injectable
    for tests; the default uses httpx with a Bearer header when ``token`` is given.
    """
    base = {"sha": sha, "repo": repo, "state": "unknown", "total": 0, "failed": 0}
    if not repo or not sha:
        return base
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "meridian-ci-verify",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        if http_get is None:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers)
        else:
            resp = await http_get(url, headers=headers)
        data = resp.json() if hasattr(resp, "json") else resp
    except Exception:  # noqa: BLE001 — never raise; CI verification is best-effort
        _log.debug("verify_commit_ci failed for %s@%s", repo, sha, exc_info=True)
        return base

    runs = (data or {}).get("check_runs") or []
    if not runs:
        return base  # no Actions check-runs for this commit → unknown
    failed = sum(
        1 for r in runs if str(r.get("conclusion") or "").lower() in _FAIL_CONCLUSIONS
    )
    pending = any(str(r.get("status") or "").lower() != "completed" for r in runs)
    if failed:
        state = "failure"
    elif pending:
        state = "pending"
    else:
        state = "success"
    return {"sha": sha, "repo": repo, "state": state, "total": len(runs), "failed": failed}
