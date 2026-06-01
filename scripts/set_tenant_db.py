"""Set neon_db_url for admin tenant accounts to MERIDIAN_PROJECT_DB.

Run after deploy to ensure admin accounts route to the project DB:
    pixi run python scripts/set_tenant_db.py

Env vars required:
    MERIDIAN_DB_URL        — auth DB connection string
    MERIDIAN_PROJECT_DB    — the project DB URL to assign to admin tenants
    MERIDIAN_ENCRYPTION_KEY — Fernet key for encrypting neon_db_url
    MERIDIAN_ADMIN_EMAILS  — comma-separated admin emails (or edit ADMIN_EMAILS below)
"""

import asyncio
import os
import sys

ADMIN_EMAILS = [
    "hello@usemeridian.us",
    "hello@usemeridian.us",
    "[admin-redacted]",
]


async def main() -> None:
    db_url = os.environ.get("MERIDIAN_DB_URL", "")
    project_db = os.environ.get("MERIDIAN_PROJECT_DB", "")

    if not db_url:
        print("ERROR: MERIDIAN_DB_URL not set", file=sys.stderr)
        sys.exit(1)
    if not project_db:
        print("ERROR: MERIDIAN_PROJECT_DB not set", file=sys.stderr)
        sys.exit(1)

    sys.path.insert(0, str(__file__).replace("scripts/set_tenant_db.py", "").replace("scripts\\set_tenant_db.py", ""))
    from meridian.pg_adapter import open_pg_connection
    from meridian.db import encrypt_field

    print("Connecting to auth DB...")
    conn = await open_pg_connection(db_url)

    emails_env = os.environ.get("MERIDIAN_ADMIN_EMAILS", "")
    emails = [e.strip() for e in emails_env.split(",") if e.strip()] if emails_env else ADMIN_EMAILS

    encrypted_url = encrypt_field(project_db)

    for email in emails:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, email, neon_db_url FROM tenants WHERE email = %s", (email,)
            )
            rows = await cur.fetchall()

        if not rows:
            print(f"  SKIP  {email} — not found in tenants table")
            continue

        tenant = rows[0]
        current = tenant.get("neon_db_url") or ""
        if current == encrypted_url:
            print(f"  OK    {email} — already set")
            continue

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE tenants SET neon_db_url = %s WHERE id = %s",
                (encrypted_url, tenant["id"]),
            )
        print(f"  SET   {email} -> neon_db_url = MERIDIAN_PROJECT_DB (encrypted)")

    await conn.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
