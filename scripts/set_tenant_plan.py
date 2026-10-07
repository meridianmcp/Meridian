"""Set or revoke a tenant's plan by account email (grant the playtester plan).

Usage (MERIDIAN_DB_URL must point at the control-plane database)::

    pixi run python scripts/set_tenant_plan.py EMAIL PLAN [--expires YYYY-MM-DD | --no-expiry]
                                               [--clear-internal] [--apply]

    # preview, writes nothing:
    pixi run python scripts/set_tenant_plan.py someone@example.com playtester
    # grant, with an end date:
    pixi run python scripts/set_tenant_plan.py someone@example.com playtester --expires 2027-01-31 --apply
    # move a staff account to playtester (also clears is_internal):
    pixi run python scripts/set_tenant_plan.py someone@example.com playtester --clear-internal --apply
    # revoke:
    pixi run python scripts/set_tenant_plan.py someone@example.com free --apply

A thin wrapper over ``meridian.tenant_plan_admin.set_tenant_plan_by_email``, the
same function the ``POST /admin/tenants/plan`` admin route calls: idempotent,
audited (``action_audit_log``), validated against ``plans.OPERATOR_SETTABLE_PLANS``.
Prints the result as JSON, including the plan label and any warning. Exit code
is 0 on success (also for a preview or a no-op), 1 for a refused request.

Deploy order: run it only after the release that knows the ``playtester`` plan
is live. An older server treats the value as an unknown plan and gives the
account Free-tier behaviour.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import selectors
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from meridian.tenant_plan_admin import UNSET, PlanChangeError, set_tenant_plan_by_email  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Set or revoke a tenant's plan by account email (see the module docstring).",
    )
    p.add_argument("email", help="account email of the tenant")
    p.add_argument("plan", help="free | standard | pro | playtester")
    end = p.add_mutually_exclusive_group()
    end.add_argument("--expires", metavar="YYYY-MM-DD", help="end date of a playtester's access (UTC)")
    end.add_argument("--no-expiry", action="store_true", help="clear a playtester's end date")
    p.add_argument("--clear-internal", action="store_true", help="also clear the is_internal (staff) flag")
    p.add_argument("--apply", action="store_true", help="write the change (default: preview only)")
    return p


async def run(args: argparse.Namespace, db: Any) -> dict[str, Any]:
    """Apply ``args`` to ``db`` (any connection the app uses); raises PlanChangeError."""
    expires: Any = UNSET
    if args.no_expiry:
        expires = None
    elif args.expires:
        expires = args.expires
    return await set_tenant_plan_by_email(
        db,
        args.email,
        args.plan,
        expires_at=expires,
        clear_internal=args.clear_internal,
        apply=args.apply,
        actor=f"cli:{os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown'}",
        via="cli",
    )


async def _amain(argv: "list[str] | None") -> int:
    args = build_parser().parse_args(argv)
    url = os.environ.get("MERIDIAN_DB_URL") or os.environ.get("MERIDIAN_AUTH_DB", "")
    if not url:
        print("ERROR: MERIDIAN_DB_URL (the control-plane database) is not set.", file=sys.stderr)
        return 1
    from meridian.pg_adapter import open_pg_connection

    db = await open_pg_connection(url)
    try:
        result = await run(args, db)
    except PlanChangeError as exc:
        print(f"ERROR ({exc.status}): {exc.message}", file=sys.stderr)
        return 1
    finally:
        await db.close()
    print(json.dumps(result, indent=2))
    if not args.apply:
        print("preview only: nothing was written; add --apply to make the change.", file=sys.stderr)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    # psycopg3 needs a selector event loop on Windows.
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    try:
        return loop.run_until_complete(_amain(argv))
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
