"""Billing, checkout, Stripe webhook, and waitlist routes."""
from __future__ import annotations

import asyncio
import html as html_module
import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .._deps import _db, _hosted_mode
from .. import db as db_module

router = APIRouter()


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------


@router.post("/waitlist", status_code=201)
async def join_waitlist(request: Request) -> dict[str, Any]:
    """POST {"email": "...", "note": "..."} — add to hosted-tier waitlist.

    Returns the created entry. 409 on duplicate email.
    """
    body = await request.json()
    email = (body.get("email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="valid email required")
    plan = (body.get("plan") or "standard").strip()
    source = (body.get("source") or "landing").strip()
    note_parts = []
    if body.get("note"):
        note_parts.append(body["note"].strip())
    note_parts.append(f"plan:{plan} source:{source}")
    note = " ".join(note_parts) if note_parts else None
    db = await _db(request)
    try:
        entry = await db_module.add_waitlist_entry(db, email, note)
    except Exception as exc:
        if "UNIQUE" in str(exc) or "unique" in str(exc):
            raise HTTPException(status_code=409, detail="email already on waitlist")
        raise
    # Fire-and-forget confirmation email — never block the response
    if _hosted_mode():
        try:
            from ..hosted import send_waitlist_confirmation_email  # noqa: PLC0415
            asyncio.create_task(send_waitlist_confirmation_email(email))
        except Exception:
            pass
    return entry


@router.get("/waitlist")
async def list_waitlist(request: Request) -> list[dict[str, Any]]:
    """GET all waitlist entries, newest first. Admin use only."""
    db = request.app.state.db
    return await db_module.get_waitlist(db)


@router.get("/admin/waitlist", response_class=HTMLResponse)
async def admin_waitlist_page(request: Request) -> HTMLResponse:
    """Admin waitlist management page — shows signups, tenant stats, approve/delete buttons."""
    from ..hosted import get_current_tenant, is_admin_db  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return HTMLResponse("<h1>403</h1><p>Not authenticated.</p>", status_code=403)
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        return HTMLResponse("<h1>403</h1><p>Admin only.</p>", status_code=403)

    db = request.app.state.db
    entries = await db_module.get_waitlist(db)

    async def _count(sql: str) -> int:
        async with db.execute(sql) as cur:
            row = await cur.fetchone()
        return (row[0] if row else 0) or 0

    total_tenants = await _count("SELECT COUNT(*) FROM tenants")
    free_tenants = await _count("SELECT COUNT(*) FROM tenants WHERE plan='free'")
    paid_tenants = await _count("SELECT COUNT(*) FROM tenants WHERE plan NOT IN ('free','') AND plan IS NOT NULL")

    rows_html = "".join(
        f"""<tr>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35">{html_module.escape(e.get("email",""))}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35;color:#8b8fa8;font-size:11px">{html_module.escape((e.get("created_at") or "")[:16])}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35;color:#8b8fa8;font-size:11px">{html_module.escape(e.get("note") or "")}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35">
            <button onclick="delWL('{html_module.escape(e.get('id',''))}',this)" style="background:#2a0f0f;border:1px solid #5a1a1a;color:#e05252;border-radius:3px;padding:2px 8px;font-size:10px;cursor:pointer">Delete</button>
          </td>
        </tr>"""
        for e in entries
    ) or "<tr><td colspan='4' style='padding:16px;text-align:center;color:#8b8fa8'>No waitlist entries.</td></tr>"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Admin — Waitlist — Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:32px 24px}}
h1{{font-size:1.4rem;margin-bottom:8px}}nav a{{color:#6c8fff;text-decoration:none;font-size:13px;margin-right:16px}}
.stats{{display:flex;gap:16px;margin:20px 0}}
.stat{{background:#16181c;border:1px solid #2a2d35;border-radius:8px;padding:12px 18px;min-width:120px}}
.stat .n{{font-size:1.6rem;font-weight:700;color:#6c8fff}}.stat .l{{font-size:11px;color:#8b8fa8;margin-top:2px}}
table{{width:100%;border-collapse:collapse;background:#16181c;border:1px solid #2a2d35;border-radius:8px;overflow:hidden;margin-top:16px}}
th{{padding:8px 10px;text-align:left;background:#1e2029;font-size:11px;color:#8b8fa8;border-bottom:1px solid #2a2d35}}
tr:hover td{{background:#1a1c23}}
</style>
</head>
<body>
<nav><a href="/dashboard">← Dashboard</a> <a href="/admin/health">Health</a></nav>
<h1 style="margin-top:16px">Waitlist Management</h1>
<p style="color:#8b8fa8;font-size:13px;margin-top:4px">{len(entries)} total signup{"s" if len(entries)!=1 else ""}</p>
<div class="stats">
  <div class="stat"><div class="n">{len(entries)}</div><div class="l">Waitlist</div></div>
  <div class="stat"><div class="n">{total_tenants}</div><div class="l">Total Tenants</div></div>
  <div class="stat"><div class="n">{free_tenants}</div><div class="l">Free Plan</div></div>
  <div class="stat"><div class="n">{paid_tenants}</div><div class="l">Paid Plan</div></div>
</div>
<table>
<thead><tr><th>Email</th><th>Signed Up</th><th>Note</th><th>Action</th></tr></thead>
<tbody id="wl-body">{rows_html}</tbody>
</table>
<script>
async function delWL(id, btn) {{
  if (!confirm('Delete this waitlist entry?')) return;
  const r = await fetch('/admin/waitlist/' + id, {{method:'DELETE'}});
  if (r.ok) {{ btn.closest('tr').remove(); }} else {{ alert('Failed: ' + r.status); }}
}}
</script>
</body></html>"""
    return HTMLResponse(html)


@router.delete("/admin/waitlist/{entry_id}")
async def admin_delete_waitlist_entry(entry_id: str, request: Request) -> dict[str, Any]:
    """Delete a waitlist entry by id. Admin only."""
    from ..hosted import get_current_tenant, is_admin_db  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(status_code=403, detail="not authenticated")
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        raise HTTPException(status_code=403, detail="admin only")
    db = request.app.state.db
    await db.execute("DELETE FROM waitlist WHERE id = ?", (entry_id,))
    await db.commit()
    return {"deleted": True, "id": entry_id}


@router.get("/waitlist-pending")
async def waitlist_pending(request: Request) -> HTMLResponse:
    """Landing page for non-admin users who sign in during pre-launch."""
    message = (request.query_params.get("message") or "").strip()
    badge = "Early access is full" if message else "✓ You're on the list"
    heading = "You're on the waitlist" if message else "Thanks for signing up!"
    body = (
        message
        if message
        else "Meridian is in early access. We'll email you when your account is ready."
    )
    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>You're on the waitlist — Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#16181c;border:1px solid #2a2d35;border-radius:12px;padding:44px 40px;
  max-width:480px;width:100%;margin:20px;text-align:center}
.logo{font-size:1.3rem;font-weight:700;color:#e8eaf0;margin-bottom:24px}
.logo span{color:#6c8fff}
h1{font-size:1.5rem;font-weight:700;margin-bottom:12px}
p{color:#8b8fa8;font-size:.9rem;line-height:1.6;margin-bottom:16px}
.badge{display:inline-block;background:#1e2029;border:1px solid #2a2d35;border-radius:20px;
  padding:6px 16px;font-size:.8rem;color:#6c8fff;margin-bottom:24px}
a{color:#6c8fff;text-decoration:none}
a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="card">
  <div class="logo">⬡ <span>Meridian</span></div>
  <div class="badge">__BADGE__</div>
  <h1>__HEADING__</h1>
  <p>__BODY__</p>
  <p>In the meantime, explore the live demo or read the docs.</p>
  <div style="display:flex;gap:10px;justify-content:center;margin-top:20px;flex-wrap:wrap">
    <a href="/" style="display:inline-block;background:#1a1c23;border:1px solid #2a2d35;border-radius:8px;padding:9px 18px;color:#e8eaf0;font-size:.85rem;text-decoration:none">← Back to home</a>
    <a href="/demo" style="display:inline-block;background:#7c3aed;border:none;border-radius:8px;padding:9px 18px;color:#fff;font-size:.85rem;text-decoration:none">→ Try the live demo</a>
    <a href="https://docs.usemeridian.us" target="_blank" style="display:inline-block;background:#1a1c23;border:1px solid #2a2d35;border-radius:8px;padding:9px 18px;color:#e8eaf0;font-size:.85rem;text-decoration:none">Read the docs</a>
  </div>
  <p style="margin-top:24px;font-size:.78rem"><a href="/auth/logout">sign out</a></p>
</div>
</body>
</html>"""
    html = (
        html.replace("__BADGE__", badge)
        .replace("__HEADING__", heading)
        .replace("__BODY__", body)
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# v1.0 — Stripe Checkout (API-based, plan-aware)
# ---------------------------------------------------------------------------


@router.get("/billing/portal")
async def billing_portal_redirect(request: Request):
    """G2.11 — open a Stripe Customer Portal session for the signed-in tenant.

    Routes to /pricing when the tenant has no stripe_customer_id (free tier
    or trial), to /auth/login when not signed in, and to the Stripe-hosted
    portal otherwise.
    """
    from fastapi.responses import RedirectResponse  # noqa: PLC0415
    from ..hosted import create_stripe_billing_portal_session, get_current_tenant  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return RedirectResponse("/auth/login?next=/billing/portal", status_code=302)

    try:
        url = await create_stripe_billing_portal_session(tenant)
    except ValueError:
        # No stripe_customer_id yet — direct the user to subscribe instead.
        return RedirectResponse("/pricing", status_code=302)
    except RuntimeError:
        # Stripe not configured (local dev) — fall through to pricing.
        return RedirectResponse("/pricing", status_code=302)

    return RedirectResponse(url, status_code=302)


@router.post("/billing/portal")
async def billing_portal_json(request: Request) -> dict[str, str]:
    """Return Stripe billing portal URL as JSON for dashboard AJAX calls (e7d4400b)."""
    from ..hosted import create_stripe_billing_portal_session, get_current_tenant  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not tenant.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="No billing account")
    try:
        url = await create_stripe_billing_portal_session(tenant)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"url": url}


@router.get("/checkout")
async def checkout_redirect(request: Request, plan: str = "standard") -> RedirectResponse:
    """Create a Stripe Checkout Session and redirect to it.

    Requires an active session cookie. ``plan`` must be ``standard`` or ``pro``.
    Falls back to the payment link if Stripe API is not configured.
    """
    from ..hosted import create_stripe_checkout_session, get_current_tenant

    if plan not in ("standard", "pro"):
        raise HTTPException(status_code=400, detail="plan must be standard or pro")

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return RedirectResponse(f"/auth/login?next=/checkout%3Fplan%3D{plan}", status_code=302)

    try:
        url = await create_stripe_checkout_session(tenant, plan)
    except RuntimeError:
        # Stripe not configured — fall back to payment link
        fallback = os.environ.get("STRIPE_PAYMENT_LINK", "/auth/login")
        return RedirectResponse(fallback, status_code=302)

    return RedirectResponse(url, status_code=302)


# ---------------------------------------------------------------------------
# v2.0 — Stripe webhook
# ---------------------------------------------------------------------------


@router.post("/webhooks/stripe", status_code=200)
async def stripe_webhook(request: Request) -> dict[str, str]:
    """Handle Stripe webhook events.

    Verifies the ``Stripe-Signature`` header against ``STRIPE_WEBHOOK_SECRET``.
    On ``checkout.session.completed`` or ``invoice.paid``:
      1. Upserts the tenant by email.
      2. Provisions a Neon DB if not already done.
      3. Creates an API bearer token and sends a welcome email.

    Returns ``{"status": "ok"}`` on success or if the event type is ignored.
    Returns 400 on signature verification failure.
    """
    import hmac as _hmac
    import hashlib as _hashlib
    import time as _time

    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    raw_body = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    # Verify signature if secret is configured
    if webhook_secret:
        try:
            _verify_stripe_signature(raw_body, sig_header, webhook_secret)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        event = json.loads(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    event_type = event.get("type", "")
    _HANDLED = {
        "checkout.session.completed",
        "invoice.paid",
        "customer.subscription.created",
        "invoice.payment_failed",
        "customer.subscription.past_due",
    }
    if event_type not in _HANDLED:
        return {"status": "ignored"}

    event_obj = event.get("data", {}).get("object", {})
    email = (
        event_obj.get("customer_email")
        or event_obj.get("customer_details", {}).get("email")
        or ""
    )
    stripe_customer_id = event_obj.get("customer", "")

    db = request.app.state.db

    # Dunning: payment failed → stamp payment_failed_at and return early.
    if event_type in ("invoice.payment_failed", "customer.subscription.past_due"):
        if stripe_customer_id:
            tenant = await db_module.get_tenant_by_stripe_customer(db, stripe_customer_id)
            if tenant and not tenant.get("payment_failed_at"):
                from datetime import datetime, timezone as _tz
                ts = datetime.now(_tz.utc).isoformat()
                await db_module.update_tenant(db, tenant["id"], payment_failed_at=ts, dunning_email_sent=0)
        return {"status": "dunning_started"}

    if not email:
        return {"status": "no_email"}

    # Resolve plan from checkout metadata (standard or pro); default to standard
    plan = event_obj.get("metadata", {}).get("plan", "standard")
    if plan not in ("standard", "pro"):
        plan = "standard"

    tenant = await db_module.upsert_tenant(db, email=email)
    if stripe_customer_id:
        tenant = await db_module.update_tenant(
            db, tenant["id"], stripe_customer_id=stripe_customer_id, plan=plan
        )
        # Payment recovered — clear any dunning state
        if tenant and tenant.get("payment_failed_at"):
            tenant = await db_module.update_tenant(
                db, tenant["id"], payment_failed_at=None, dunning_email_sent=0
            )

    # Extract metered subscription item ID when overage price is configured.
    # Best-effort — never block provisioning if this fails.
    from ..hosted import STRIPE_OVERAGE_PRICE_ID as _OVERAGE_PRICE_ID
    subscription_id = event_obj.get("subscription")
    if subscription_id and _OVERAGE_PRICE_ID and stripe_customer_id:
        try:
            import stripe as _stripe
            _stripe.api_key = os.environ.get("STRIPE_API_KEY", "")
            sub = _stripe.Subscription.retrieve(subscription_id)
            metered_item_id = next(
                (i.id for i in sub.items.data if i.price.id == _OVERAGE_PRICE_ID),
                None,
            )
            if metered_item_id:
                await db_module.update_tenant(
                    db, tenant["id"], stripe_metered_item_id=metered_item_id
                )
        except Exception:  # noqa: BLE001
            pass

    # Capacity check before provisioning
    from ..hosted import check_capacity, provision_neon_db, send_welcome_email
    try:
        await check_capacity(db)
    except RuntimeError as cap_exc:
        import logging
        logging.getLogger(__name__).error("Capacity exceeded: %s", cap_exc)
        return {"status": "capacity_exceeded"}

    # Provision Neon DB
    try:
        tenant = await provision_neon_db(tenant["id"], db)
    except Exception as exc:
        # Log but don't fail the webhook — Stripe will retry
        import logging
        logging.getLogger(__name__).error("Neon provisioning failed for %s: %s", email, exc)
        return {"status": "provisioning_queued"}

    # Create API token + send welcome email
    raw_token, _token_row = await db_module.create_api_token(db, tenant["id"], label="welcome")
    try:
        await send_welcome_email(email, raw_token, tenant)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Welcome email failed for %s: %s", email, exc)

    return {"status": "ok"}


def _verify_stripe_signature(raw_body: bytes, sig_header: str, secret: str) -> None:
    """Verify a Stripe webhook signature. Raises ValueError on failure."""
    import hmac
    import hashlib
    import time

    parts = {k: v for part in sig_header.split(",") for k, v in [part.split("=", 1)] if "=" in part}
    timestamp = parts.get("t", "")
    sig = parts.get("v1", "")
    if not timestamp or not sig:
        raise ValueError("missing signature components")

    try:
        ts = int(timestamp)
    except ValueError:
        raise ValueError("invalid timestamp")

    tolerance = 300  # 5 minutes
    if abs(time.time() - ts) > tolerance:
        raise ValueError("webhook timestamp too old")

    payload = f"{timestamp}.{raw_body.decode()}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("signature mismatch")
