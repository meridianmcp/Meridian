"""Add a workspace member directly to the auth DB (bypasses invite email).

Usage:
    pixi run python scripts/add_workspace_member.py \
        --owner ajc123private@gmail.com \
        --invitee mytf2tradingemail@gmail.com \
        --role member \
        --github-access read

Credentials come from .env via load_dotenv().
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone


def main_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--owner", default="ajc123private@gmail.com")
    p.add_argument("--invitee", default="mytf2tradingemail@gmail.com")
    p.add_argument("--role", default="member")
    p.add_argument("--github-access", default="read")
    return p.parse_args()


async def run(owner: str, invitee: str, role: str, github_access: str) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from meridian.toml_config import get_active_db_url
    from meridian.pg_adapter import open_pg_connection

    auth_url = os.environ.get("MERIDIAN_DB_URL") or get_active_db_url()[0]
    if not auth_url:
        print("ERROR: no auth DB URL (set MERIDIAN_DB_URL or meridian.toml)", file=sys.stderr)
        return 2

    conn = await open_pg_connection(auth_url)
    try:
        # Find owner tenant
        async with conn.execute(
            "SELECT id, email, plan FROM tenants WHERE email = %s", (owner,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            print(f"ERROR: owner tenant not found for email={owner}", file=sys.stderr)
            return 1
        tenant_id = row["id"]
        print(f"Owner tenant: {row['email']} (id={tenant_id}, plan={row['plan']})")

        # Check existing membership
        async with conn.execute(
            "SELECT id, role, joined_at FROM workspace_members WHERE tenant_id = %s AND email = %s",
            (tenant_id, invitee),
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            print(
                f"Member {invitee} already exists: id={existing['id']}, "
                f"role={existing['role']}, joined_at={existing['joined_at']}"
            )
            if existing["joined_at"] is None:
                # Pending invite — mark as accepted
                now = datetime.now(timezone.utc).isoformat()
                await conn.execute(
                    "UPDATE workspace_members SET joined_at = %s, token_hash = NULL WHERE id = %s",
                    (now, existing["id"]),
                )
                print(f"  → Marked pending invite as accepted (joined_at={now})")
            return 0

        # Insert accepted member (no pending invite flow — direct insert)
        mid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, token_hash, invited_at, joined_at) "
            "VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)",
            (mid, tenant_id, invitee, role, github_access, now, now),
        )
        print(
            f"Inserted workspace_members row: id={mid}, "
            f"tenant_id={tenant_id}, email={invitee}, role={role}, "
            f"github_access={github_access}, joined_at={now}"
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    args = main_args()
    sys.exit(asyncio.run(run(args.owner, args.invitee, args.role, args.github_access)))
