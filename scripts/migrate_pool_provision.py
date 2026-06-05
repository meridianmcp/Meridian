"""
Provision Neon databases for existing tenants with null pool_project_id.

Usage:
    pixi run python scripts/migrate_pool_provision.py [--dry-run] [--email EMAIL] [--tenant-id TENANT_ID]

The script connects to MERIDIAN_DB_URL and processes every tenant where:
  - pool_project_id IS NULL
  - plan != 'admin'

Behaviour per tenant:
  - neon_project_id already set → legacy pre-pool tenant; skipped with a warning
    (these need manual migration via Neon console)
  - neon_project_id NULL, plan == 'pro', NEON_API_KEY_PRO not set → deferred;
    a HITL request is filed via the Meridian API and the tenant is skipped
  - otherwise → calls provision_neon_db (standard pool architecture)

Safe to re-run: provision_neon_db is idempotent (no-ops if already provisioned).
"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys
from pathlib import Path

asyncio.set_event_loop(asyncio.SelectorEventLoop(selectors.SelectSelector()))

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

# Load .env
_env_path = _REPO / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"'))
os.environ.setdefault("NEON_API_KEY", os.environ.get("MERIDIAN_STANDARD_KEY", ""))
os.environ.setdefault("NEON_API_KEY_PRO", os.environ.get("MERIDIAN_PRO_KEY", ""))

DRY_RUN = "--dry-run" in sys.argv


def _arg_value(flag: str) -> str:
    if flag not in sys.argv:
        return ""
    idx = sys.argv.index(flag)
    if idx + 1 >= len(sys.argv):
        print(f"ERROR: {flag} requires a value.")
        sys.exit(1)
    return sys.argv[idx + 1].strip()


EMAIL_FILTER = _arg_value("--email")
TENANT_ID_FILTER = _arg_value("--tenant-id")

MERIDIAN_DB_URL = os.environ.get("MERIDIAN_DB_URL") or os.environ.get("MERIDIAN_AUTH_DB", "")
if not MERIDIAN_DB_URL:
    print("ERROR: MERIDIAN_DB_URL or MERIDIAN_AUTH_DB not set in .env — aborting.")
    sys.exit(1)

MERIDIAN_BASE_URL = os.environ.get("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")
MERIDIAN_PROJECT_ID = os.environ.get("MERIDIAN_PROJECT_ID", "")


async def _file_hitl(message: str) -> None:
    """POST a HITL request to the running Meridian server (best-effort)."""
    if not MERIDIAN_PROJECT_ID:
        print(f"  [HITL skipped - MERIDIAN_PROJECT_ID not set] {message}")
        return
    try:
        import httpx
        url = f"{MERIDIAN_BASE_URL}/projects/{MERIDIAN_PROJECT_ID}/hitl"
        r = httpx.post(url, json={"question": message}, timeout=10)
        if r.status_code == 200:
            print(f"  [HITL filed] {message[:80]}")
        else:
            print(f"  [HITL POST returned {r.status_code}] {message[:80]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [HITL POST failed: {exc}] {message[:80]}")


async def run() -> None:
    from meridian.pg_adapter import open_pg_connection
    from meridian import hosted

    print("Connecting to MERIDIAN_DB_URL...")
    db = await open_pg_connection(MERIDIAN_DB_URL)

    # Fetch all tenants that might need provisioning
    query = (
        "SELECT id, email, plan, neon_project_id, pool_project_id FROM tenants "
        "WHERE pool_project_id IS NULL AND plan != 'admin'"
    )
    params: list[str] = []
    if EMAIL_FILTER:
        query += " AND LOWER(email) = LOWER(%s)"
        params.append(EMAIL_FILTER)
    if TENANT_ID_FILTER:
        query += " AND id = %s"
        params.append(TENANT_ID_FILTER)

    rows = await db.execute(query, tuple(params))
    tenants = await rows.fetchall()

    if not tenants:
        scope = EMAIL_FILTER or TENANT_ID_FILTER or "all tenants"
        print(f"No tenants with null pool_project_id found for {scope}. Nothing to do.")
        await db.close()
        return

    print(f"Found {len(tenants)} tenant(s) with pool_project_id IS NULL (plan != admin).\n")

    stats = {"provisioned": 0, "skipped_legacy": 0, "skipped_pro_key": 0, "errors": 0}
    pro_key_missing = False

    for tenant in tenants:
        tid = tenant["id"]
        email = tenant["email"]
        plan = tenant["plan"] or "free"
        has_neon = bool(tenant["neon_project_id"])

        print(f"  [{plan:8s}] {email} ({tid[:8]}...)")

        # Legacy pre-pool tenant: has a neon_project_id but no pool_project_id.
        # These were provisioned before the pool architecture was introduced and
        # need a manual console migration (copy neon_project_id into a pool row).
        if has_neon:
            print(f"           -> SKIP: already has neon_project_id={tenant['neon_project_id'][:8]}... "
                  f"(legacy direct-project tenant - manual migration required)")
            stats["skipped_legacy"] += 1
            continue

        # Pro tenants require NEON_API_KEY_PRO — defer if missing.
        if plan == "pro" and not os.environ.get("NEON_API_KEY_PRO"):
            if not pro_key_missing:
                pro_key_missing = True
                await _file_hitl(
                    "migrate_pool_provision.py: NEON_API_KEY_PRO is not set but there are "
                    "pro tenants with pool_project_id=NULL that need provisioning. "
                    "Please set NEON_API_KEY_PRO in the production environment and re-run "
                    "scripts/migrate_pool_provision.py."
                )
            print("           -> DEFERRED: NEON_API_KEY_PRO not set (HITL filed)")
            stats["skipped_pro_key"] += 1
            continue

        if DRY_RUN:
            print("           -> DRY RUN: would call provision_neon_db")
            continue

        try:
            updated = await hosted.provision_neon_db(tid, db)
            new_pool = updated.get("pool_project_id", "?")
            new_neon = updated.get("neon_project_id", "?")
            print(f"           -> OK: pool={str(new_pool)[:8]}... neon={str(new_neon)[:8]}...")
            stats["provisioned"] += 1
        except Exception as exc:  # noqa: BLE001
            print(f"           -> ERROR: {exc}")
            stats["errors"] += 1

    await db.close()

    print(f"""
Summary:
  Provisioned:        {stats['provisioned']}
  Skipped (legacy):   {stats['skipped_legacy']}  <- manual Neon console migration needed
  Deferred (pro key): {stats['skipped_pro_key']}  <- set NEON_API_KEY_PRO and re-run
  Errors:             {stats['errors']}
""")
    if stats["errors"]:
        sys.exit(1)


asyncio.get_event_loop().run_until_complete(run())
