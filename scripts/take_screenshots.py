"""Take product screenshots for docs using Playwright.

Run:  pixi run python scripts/take_screenshots.py [--url http://localhost:7878]

Saves PNG files to docs/screenshots/.  Requires a running Meridian server.
Install Playwright browsers first if needed:
  pixi run python -m playwright install chromium --with-deps

Screenshots taken:
  landing_hero.png     — landing page above the fold
  landing_features.png — features / terminal section (scrolled)
  demo_project.png     — /demo with projects list
  demo_session.png     — /demo with first session selected
  demo_queue.png       — /demo queue tab
  demo_hitl.png        — /demo HITL tab
  pricing.png          — /pricing page
  install_mcp.png      — /install-mcp onboarding page
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCREENSHOTS_DIR = BASE_DIR / "docs" / "screenshots"


async def run(base_url: str) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright not installed. Run: pixi run python -m playwright install chromium")
        sys.exit(1)

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Saving screenshots to {SCREENSHOTS_DIR}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            color_scheme="dark",
        )

        async def shot(path: str, filename: str, wait: float = 3.0,
                       scroll_to: int = 0, clip: dict | None = None) -> None:
            page = await ctx.new_page()
            full_url = base_url.rstrip("/") + path
            print(f"  {filename}: {full_url}", flush=True)
            try:
                await page.goto(full_url, wait_until="networkidle", timeout=30_000)
                if scroll_to:
                    await page.evaluate(f"window.scrollTo(0, {scroll_to})")
                await asyncio.sleep(wait)
                dest = str(SCREENSHOTS_DIR / filename)
                if clip:
                    await page.screenshot(path=dest, clip=clip)
                else:
                    await page.screenshot(path=dest, full_page=False)
                size_kb = os.path.getsize(dest) // 1024
                print(f"    -> saved ({size_kb} KB)")
            except Exception as e:
                print(f"    ERROR: {e}")
            finally:
                await page.close()

        # Landing page — hero section
        await shot("/", "landing_hero.png", wait=2.0)

        # Landing page — features section (scroll down ~800px)
        await shot("/", "landing_features.png", wait=2.0, scroll_to=800)

        # /demo — project list visible, no session selected
        await shot("/demo", "demo_project.png", wait=3.5)

        # /demo — click first session to select it, then screenshot
        page = await ctx.new_page()
        try:
            await page.goto(base_url.rstrip("/") + "/demo",
                            wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(3.5)
            # Click the first session row in the left panel
            sess_sel = ".session-row, .live-session-row, [data-session-id]"
            try:
                await page.locator(sess_sel).first.click(timeout=5_000)
                await asyncio.sleep(1.5)
            except Exception:
                pass
            await page.screenshot(path=str(SCREENSHOTS_DIR / "demo_session.png"))
            print("  demo_session.png -> saved")

            # Queue tab
            try:
                await page.locator('[data-vtab="queue"], button[title*="Queue"]').first.click(timeout=5_000)
                await asyncio.sleep(2.0)
            except Exception:
                pass
            await page.screenshot(path=str(SCREENSHOTS_DIR / "demo_queue.png"))
            print("  demo_queue.png -> saved")

            # HITL tab
            try:
                await page.locator('[data-vtab="hitl"], button[title*="HITL"]').first.click(timeout=5_000)
                await asyncio.sleep(1.5)
            except Exception:
                pass
            await page.screenshot(path=str(SCREENSHOTS_DIR / "demo_hitl.png"))
            print("  demo_hitl.png -> saved")
        except Exception as e:
            print(f"  demo tabs ERROR: {e}")
        finally:
            await page.close()

        # /pricing
        await shot("/pricing", "pricing.png", wait=2.0)

        # /install-mcp
        await shot("/install-mcp", "install_mcp.png", wait=1.5)

        await browser.close()

    print(f"\nDone. Screenshots in {SCREENSHOTS_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Take Meridian product screenshots")
    parser.add_argument("--url", default="http://localhost:7878",
                        help="Base URL of running Meridian server (default: http://localhost:7878)")
    args = parser.parse_args()
    asyncio.run(run(args.url))


if __name__ == "__main__":
    main()
