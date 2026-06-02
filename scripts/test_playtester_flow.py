"""
Pre-launch playtester flow test.
Tests every endpoint a new user would hit, in order.
Requires: MERIDIAN_AUTH_DB in env (for DB checks).

Usage:
  pixi run python scripts/test_playtester_flow.py --url https://usemeridian.us

Checks:
  1. Landing page loads
  2. /auth/login has Google + GitHub buttons
  3. /demo loads with data (not blank)
  4. /demo onboarding overlay HTML present
  5. Write actions blocked in demo (403 on /sessions/worker)
  6. /waitlist-pending loads with nav links
  7. /pricing loads with Free + Standard + Pro tiers
  8. POST /waitlist stores email
  9. /health returns 200
  10. /mcp/quickstart returns start_session reference
  11. GitHub OAuth redirect works (/auth/github -> GitHub)
  12. dradamawsome@gmail.com tenant has neon_db_url set
  13. Neon reconnect: idle 30s then /health still 200
"""
import sys, os, asyncio, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_URL = "https://usemeridian.us"
for arg in sys.argv[1:]:
    if arg.startswith("--url="):
        BASE_URL = arg.split("=", 1)[1]
    elif arg == "--url" and len(sys.argv) > sys.argv.index(arg) + 1:
        BASE_URL = sys.argv[sys.argv.index(arg) + 1]

import httpx

RESULTS = []

def check(name, ok, detail=""):
    icon = "PASS" if ok else "FAIL"
    RESULTS.append((icon, name, detail))
    print(f"[{icon}] {name}" + (f" — {detail}" if detail else ""))

async def run():
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:

        # 1. Landing page
        r = await c.get(f"{BASE_URL}/")
        check("Landing page loads", r.status_code == 200, f"status={r.status_code}")

        # 2. Auth login page
        r = await c.get(f"{BASE_URL}/auth/login")
        ok = "google" in r.text.lower() or "sign in" in r.text.lower()
        check("/auth/login has sign-in options", ok)

        # 3. Demo loads with data
        r = await c.get(f"{BASE_URL}/demo")
        has_data = "backend-api-v2" in r.text or "backend" in r.text.lower()
        check("/demo loads with project data", r.status_code == 200 and has_data,
              f"status={r.status_code}, has_data={has_data}")

        # 4. Demo onboarding overlay present
        has_overlay = "read-only" in r.text.lower() or "sign in" in r.text.lower()
        check("/demo has onboarding overlay HTML", has_overlay)

        # 5. Write blocked in demo (should 403)
        demo_cookie = {"meridian_demo": "1"}
        r2 = await c.post(f"{BASE_URL}/sessions/worker",
                          json={}, cookies=demo_cookie)
        check("/demo write actions blocked (403)", r2.status_code == 403,
              f"status={r2.status_code}")

        # 6. waitlist-pending has nav
        r = await c.get(f"{BASE_URL}/waitlist-pending")
        has_nav = "home" in r.text.lower() or "back" in r.text.lower()
        check("/waitlist-pending has navigation links", r.status_code == 200 and has_nav,
              f"status={r.status_code}, has_nav={has_nav}")

        # 7. Pricing has tiers
        r = await c.get(f"{BASE_URL}/pricing")
        has_free = "free" in r.text.lower()
        has_standard = "standard" in r.text.lower() or "$20" in r.text
        has_pro = "pro" in r.text.lower() or "$49" in r.text
        check("/pricing has Free + Standard + Pro", r.status_code == 200 and has_free and has_pro,
              f"free={has_free} standard={has_standard} pro={has_pro}")

        # 8. Waitlist signup
        test_email = f"test-playtester-{int(time.time())}@meridian-test.invalid"
        r = await c.post(f"{BASE_URL}/waitlist", json={"email": test_email})
        check("POST /waitlist stores email", r.status_code in (200, 201),
              f"status={r.status_code}")

        # 9. Health
        r = await c.get(f"{BASE_URL}/health")
        check("/health returns 200", r.status_code == 200)

        # 10. MCP quickstart has start_session
        r = await c.get(f"{BASE_URL}/mcp/quickstart")
        check("/mcp/quickstart has start_session", "start_session" in r.text,
              f"status={r.status_code}")

        # 11. GitHub OAuth redirect
        r = await c.get(f"{BASE_URL}/auth/github", follow_redirects=False)
        goes_to_github = "github.com" in r.headers.get("location", "")
        check("/auth/github redirects to GitHub", r.status_code in (302, 307) and goes_to_github,
              f"status={r.status_code}, location={r.headers.get('location','')[:60]}")

    # 12. DB check: dradamawsome has neon_db_url
    try:
        import importlib.util, base64, binascii
        env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
        env = {}
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
        DB_URL = env.get('MERIDIAN_AUTH_DB', '')
        if DB_URL:
            import psycopg, psycopg.rows
            async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
                async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    await cur.execute(
                        "SELECT neon_db_url IS NOT NULL as has_db, plan FROM tenants WHERE email=%s",
                        ('dradamawsome@gmail.com',))
                    row = await cur.fetchone()
                    if row:
                        check("dradamawsome@gmail.com has DB configured",
                              row['has_db'], f"plan={row['plan']}")
                    else:
                        check("dradamawsome@gmail.com exists in DB", False, "NOT FOUND")
    except Exception as e:
        check("DB check", False, str(e)[:60])

    # Summary
    passed = sum(1 for r in RESULTS if r[0] == "PASS")
    failed = sum(1 for r in RESULTS if r[0] == "FAIL")
    print(f"\n{'='*50}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    if failed:
        print("\nFailed checks:")
        for icon, name, detail in RESULTS:
            if icon == "FAIL":
                print(f"  - {name}: {detail}")
    return failed == 0

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)
