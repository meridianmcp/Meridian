"""
Take dashboard screenshots for README.
Assumes Meridian running at localhost:7878 with meridian-build project.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

PROJECT_ID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
BASE = f"http://localhost:7878"
OUT = Path(r"C:\Users\13144\Documents\Meridian\repository\docs\screenshots")

async def shot(page, name):
    await page.wait_for_timeout(800)
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
    print(f"  {name}.png")

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 820})
        
        print("Loading dashboard...")
        await page.goto(f"{BASE}/dashboard")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(2500)
        
        # Dashboard overview (sidebar + default)
        await shot(page, "01_dashboard")
        
        # Click the meridian-build project
        project_item = await page.query_selector(f'[data-id="{PROJECT_ID}"], .project-item')
        if project_item:
            await project_item.click()
            await page.wait_for_timeout(1500)
            await shot(page, "02_live_tab")
            
            # Goal tab
            g = await page.query_selector('[data-vtab="goal"]')
            if g:
                await g.click()
                await page.wait_for_timeout(800)
                await shot(page, "03_goal_tab")
            
            # Queue tab
            q = await page.query_selector('[data-vtab="queue"]')
            if q:
                await q.click()
                await page.wait_for_timeout(800)
                await shot(page, "04_queue_tab")
            
            # Rewind tab — click 7d
            r = await page.query_selector('[data-vtab="rewind"]')
            if r:
                await r.click()
                await page.wait_for_timeout(600)
                day_btn = await page.query_selector('.rewind-day-btn')
                if day_btn:
                    await day_btn.click()
                    await page.wait_for_timeout(2000)
                await shot(page, "05_rewind_tab")

            # Files tab
            f = await page.query_selector('[data-vtab="files"]')
            if f:
                await f.click()
                await page.wait_for_timeout(600)
                await shot(page, "06_files_tab")
        else:
            print("  No project found — dashboard may be empty or server not running")

        await browser.close()
    print(f"\nDone. Files in {OUT}")

asyncio.run(main())
