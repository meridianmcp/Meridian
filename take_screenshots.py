"""
Take dashboard screenshots for README.
Assumes Meridian running at localhost:7878 with meridian-build project.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

PROJECT_ID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
BASE = "http://localhost:7878"
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

        # Dashboard overview (before any project is open)
        await shot(page, "01_dashboard")

        # Open meridian-build programmatically via the dashboard API
        # so we don't depend on sidebar click or browser state.
        await page.evaluate(f"""
            (async () => {{
                const projects = await fetch('/projects').then(r => r.json());
                const p = projects.find(x => x.id === '{PROJECT_ID}');
                if (p && typeof openTab === 'function') openTab(p);
            }})();
        """)
        await page.wait_for_timeout(3000)

        await shot(page, "02_live_tab")

        pid = PROJECT_ID

        async def vtab(tab_name):
            sel = f'#vtab-strip-{pid} [data-vtab="{tab_name}"]'
            await page.wait_for_selector(sel, timeout=5000)
            await page.click(sel)
            await page.wait_for_timeout(1000)

        await vtab("goal")
        await shot(page, "03_goal_tab")

        await vtab("queue")
        await shot(page, "04_queue_tab")

        await vtab("rewind")
        await page.wait_for_timeout(600)
        day_btn_sel = f'.rewind-day-btn[data-pid="{pid}"][data-days="7"]'
        await page.click(day_btn_sel)
        await page.wait_for_timeout(3000)
        await shot(page, "05_rewind_tab")

        # Charts subtab within rewind
        charts_btn = await page.query_selector('.rewind-subtab-btn[data-tab="charts"]')
        if charts_btn:
            await charts_btn.click()
            await page.wait_for_timeout(1500)
            await shot(page, "05b_charts_tab")

        await vtab("files")
        await shot(page, "06_files_tab")

        await browser.close()
    print(f"\nDone. Files in {OUT}")

asyncio.run(main())
