"""G6.27 — Capture a real dashboard screenshot for the landing page hero.

Loads the seeded /demo dashboard, lets the live + HITL widgets settle, then
crops the viewport to a hero-friendly aspect ratio (3:2). Writes both a
full-width PNG and a 1600-wide variant for retina at meridian/static/.

Usage:
    pixi run python scripts/capture_solution_screenshot.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import os
BASE_URL = os.environ.get("CAPTURE_BASE_URL", "https://meridian-preview.fly.dev")
OUT_DIR = Path(__file__).resolve().parents[1] / "meridian" / "static"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_full = OUT_DIR / "the-solution-dashboard.png"
    out_card = OUT_DIR / "the-solution-dashboard-card.png"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 1050},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.goto(f"{BASE_URL}/demo", wait_until="domcontentloaded")
        # Let the SPA hydrate, seed sessions/HITL widgets render.
        page.wait_for_timeout(5000)
        # Open the first two projects as tabs so the tab strip shows the
        # multi-project coordination story (Adam's G6.27 spec). Clicking the
        # sidebar rows drives the same openTab() path as a real user; we open
        # the second then the first so backend-api-v2 lands active.
        page.evaluate(
            "() => { const rows = [...document.querySelectorAll('.project-item')];"
            "  if (rows[1]) rows[1].click();"
            "  if (rows[0]) rows[0].click(); }"
        )
        page.wait_for_timeout(1200)
        # Dismiss the demo onboarding overlay so it doesn't cover the dash.
        page.evaluate(
            "() => { const e = document.querySelector('#demo-onboarding-overlay');"
            "  if (e) e.remove(); }"
        )
        page.evaluate(
            "() => { const e = document.querySelector('#conn-setup-modal');"
            "  if (e) e.remove(); }"
        )
        page.wait_for_timeout(500)
        page.screenshot(path=str(out_full), full_page=False)
        # A tighter crop for use on the landing card grid — top 720px is the
        # most information-dense area (tab strip, status drawer, sessions).
        page.screenshot(
            path=str(out_card),
            clip={"x": 0, "y": 0, "width": 1600, "height": 980},
        )
        browser.close()

    full_kb = out_full.stat().st_size // 1024
    card_kb = out_card.stat().st_size // 1024
    print(f"wrote {out_full.name} ({full_kb} KB)")
    print(f"wrote {out_card.name} ({card_kb} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
