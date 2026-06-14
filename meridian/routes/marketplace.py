"""GitHub Marketplace webhook (be2461ed).

Receives marketplace_purchase events (purchased / cancelled / changed) from a
GitHub Marketplace listing. Verifies the X-Hub-Signature-256 header against
GITHUB_MARKETPLACE_WEBHOOK_SECRET and returns 200 immediately. Event handling is
stubbed (log + 200) for now — provisioning/expiry/plan-change logic lands later.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
_log = logging.getLogger(__name__)


def _verify_github_signature(raw_body: bytes, sig_header: str, secret: str) -> None:
    """Verify GitHub's ``X-Hub-Signature-256`` header. Raises ValueError on failure.

    The header is ``sha256=<hex>`` where ``<hex>`` is HMAC-SHA256 of the raw
    request body keyed by the webhook secret. Compared in constant time.
    """
    if not sig_header.startswith("sha256="):
        raise ValueError("missing or malformed X-Hub-Signature-256")
    sent = sig_header[len("sha256="):]
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sent):
        raise ValueError("signature mismatch")


@router.post("/webhooks/github-marketplace", status_code=200)
async def github_marketplace_webhook(request: Request) -> dict[str, Any]:
    """Handle a GitHub Marketplace ``marketplace_purchase`` event.

    Verifies ``X-Hub-Signature-256`` against ``GITHUB_MARKETPLACE_WEBHOOK_SECRET``
    (returns 401 on a bad signature), then dispatches purchased / cancelled /
    changed to stub handlers that log and return 200. The ``X-GitHub-Delivery``
    id is logged for idempotency/traceability.
    """
    secret = os.environ.get("GITHUB_MARKETPLACE_WEBHOOK_SECRET", "")
    raw_body = await request.body()
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")

    # Verify the signature when a secret is configured. A bad signature is 401.
    if secret:
        try:
            _verify_github_signature(raw_body, sig_header, secret)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        event = json.loads(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    action = (event.get("action") or "").strip()
    purchase = event.get("marketplace_purchase") or {}
    account = (purchase.get("account") or {}).get("login") or "?"

    # Stub handlers — log + 200. Real provisioning/expiry/plan-change comes later.
    if action == "purchased":
        _log.info("github-marketplace purchased: account=%s delivery=%s", account, delivery)
    elif action == "cancelled":
        _log.info("github-marketplace cancelled: account=%s delivery=%s", account, delivery)
    elif action == "changed":
        _log.info("github-marketplace changed: account=%s delivery=%s", account, delivery)
    else:
        _log.info("github-marketplace ignored action=%r delivery=%s", action, delivery)

    return {"status": "ok", "action": action}
