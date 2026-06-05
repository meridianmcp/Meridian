"""Read-only tenant-isolation audit.

The core multi-tenant invariant: NO tenant's neon_db_url may resolve to the
shared auth DB host (the "autumn-pine" endpoint that holds the tenants
table). Each tenant must live in its own isolated database — a dedicated
Neon project (paid) or an isolated pool database (free). A tenant whose DB
host equals the auth host would mean its data lives in the shared auth DB,
which is an isolation leak.

This audits every tenant and flags any that share the auth host or fail to
decrypt.

SECURITY: never prints decrypted connection strings. Only the bare host
(no userinfo, no password, no query) and boolean flags are emitted.

Usage:
    pixi run python scripts/check_decrypt_all.py

Credentials come from .env via load_dotenv() only:
    MERIDIAN_ENCRYPTION_KEY  — Fernet key
    MERIDIAN_DB_URL / meridian.toml active connection — auth DB (tenants table)
"""

import asyncio
import os
import sys
from urllib.parse import urlsplit


def _host_only(url: str | None) -> str:
    """Return just the hostname of a connection URL — never userinfo/password."""
    if not url:
        return ""
    try:
        return urlsplit(url).hostname or ""
    except Exception:
        return ""


async def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from meridian.toml_config import get_active_db_url
    from meridian.db import decrypt_field
    from meridian.pg_adapter import open_pg_connection

    auth_url = os.environ.get("MERIDIAN_DB_URL") or get_active_db_url()[0]
    if not auth_url:
        print("ERROR: no auth DB URL (set MERIDIAN_DB_URL or meridian.toml)", file=sys.stderr)
        return 2

    auth_host = _host_only(auth_url)
    # The auth DB lives on the shared "autumn-pine" endpoint; no tenant DB
    # may resolve to that same host.
    print(f"Auth DB host (shared):  {auth_host}")
    print("-" * 70)

    conn = await open_pg_connection(auth_url)
    leaks = 0
    total = 0
    try:
        async with conn.execute(
            "SELECT email, plan, neon_project_id, pool_project_id, neon_db_url "
            "FROM tenants ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()

        total = len(rows)
        for t in rows:
            email = (t.get("email") or "")[:24]
            try:
                decrypted = decrypt_field(t.get("neon_db_url"))
            except Exception as exc:
                print(f"  [FAIL] {email:24s} — decrypt error: {type(exc).__name__}")
                leaks += 1
                continue

            tenant_host = _host_only(decrypted)
            on_auth_host = bool(tenant_host) and tenant_host == auth_host
            if on_auth_host:
                leaks += 1
                verdict = "LEAK: shares auth host"
            elif not tenant_host:
                verdict = "no dedicated DB (admin/unprovisioned fallback)"
            else:
                verdict = "isolated"
            kind = (
                "pool" if t.get("pool_project_id")
                else "dedicated" if t.get("neon_project_id")
                else "none"
            )
            status = "FAIL" if on_auth_host else "PASS"
            print(
                f"  [{status}] {email:24s} plan={str(t.get('plan')):9s} "
                f"db={kind:9s} host={tenant_host or '(none)':30s} {verdict}"
            )
    finally:
        await conn.close()

    print("-" * 70)
    print(f"Audited {total} tenant(s); {leaks} isolation leak(s).")
    print("RESULT:", "NO LEAKS — all tenant DBs isolated from auth host" if leaks == 0
          else "ISOLATION LEAK — see FAIL rows")
    return 0 if leaks == 0 else 1


if __name__ == "__main__":
    # psycopg requires SelectorEventLoop on Windows (ProactorEventLoop is
    # incompatible with psycopg async mode).
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
