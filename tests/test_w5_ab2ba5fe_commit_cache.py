"""ab2ba5fe — add_sprint_item's drift check fetched recent commits on every call
(GitHub round-trip / `git log` subprocess). _fetch_recent_commits now caches per
project for a short TTL so a burst of adds shares one fetch."""
from __future__ import annotations

import pytest

from meridian.mcp import handler as mh


@pytest.mark.asyncio
async def test_fetch_recent_commits_caches_per_project(monkeypatch):
    mh._recent_commits_cache.clear()
    calls = {"n": 0}

    async def fake_uncached(project, tenant):
        calls["n"] += 1
        return [{"sha": "abc123", "message": "hello"}]

    monkeypatch.setattr(mh, "_fetch_recent_commits_uncached", fake_uncached)
    proj = {"id": "proj-x"}

    r1 = await mh._fetch_recent_commits(proj, None)
    r2 = await mh._fetch_recent_commits(proj, None)
    assert r1 == r2 == [{"sha": "abc123", "message": "hello"}]
    # The second call within the TTL is served from cache — only one real fetch.
    assert calls["n"] == 1

    # A different project is a distinct cache key → its own fetch.
    await mh._fetch_recent_commits({"id": "proj-y"}, None)
    assert calls["n"] == 2
    mh._recent_commits_cache.clear()


@pytest.mark.asyncio
async def test_fetch_recent_commits_expired_ttl_refetches(monkeypatch):
    mh._recent_commits_cache.clear()
    calls = {"n": 0}

    async def fake_uncached(project, tenant):
        calls["n"] += 1
        return [{"sha": "s", "message": "m"}]

    monkeypatch.setattr(mh, "_fetch_recent_commits_uncached", fake_uncached)
    proj = {"id": "proj-z"}
    await mh._fetch_recent_commits(proj, None)
    assert calls["n"] == 1
    # Force the cached entry to look older than the TTL → next call re-fetches.
    ts, data = mh._recent_commits_cache["proj-z"]
    mh._recent_commits_cache["proj-z"] = (ts - (mh._RECENT_COMMITS_TTL + 1), data)
    await mh._fetch_recent_commits(proj, None)
    assert calls["n"] == 2
    mh._recent_commits_cache.clear()
