"""Regression: every static image the landing page references must be committed
and actually served.

Guards against the class of bug where the landing markup points at an asset that
was never committed (e.g. dashboard-hero-screenshot.png), so the live page shows
a broken image icon instead of the screenshot. See sprint item e352bf7a-adjacent
landing hotfix (dashboard-queue.png).
"""
from __future__ import annotations

import re


_ASSET_RE = re.compile(r"/static/[\w./-]+\.(?:png|jpg|jpeg|svg|webp|ico)")


def test_landing_referenced_static_images_are_served(client):
    """Each /static/<image> referenced by the landing page returns 200."""
    r = client.get("/")
    assert r.status_code == 200

    refs = sorted(set(_ASSET_RE.findall(r.text)))
    assert refs, "landing page should reference at least one static image"

    for path in refs:
        resp = client.get(path)
        assert resp.status_code == 200, (
            f"{path} is referenced by the landing page but not served "
            f"({resp.status_code}) — is the asset committed to meridian/static/?"
        )
        assert len(resp.content) > 0, f"{path} served but is empty"


def test_landing_solution_showcase_uses_committed_screenshot(client):
    """The solution-showcase <img> points at the committed dashboard screenshot,
    and the tag is well-formed (no stray literal escape sequences)."""
    r = client.get("/")
    assert r.status_code == 200
    assert "/static/dashboard-queue.png" in r.text
    # The e89daf1 hotfix once wedged a literal "\\r\\n" into the <img> tag.
    assert "dashboard-queue.png\"\\r\\n" not in r.text
    assert "dashboard-hero-screenshot.png" not in r.text
