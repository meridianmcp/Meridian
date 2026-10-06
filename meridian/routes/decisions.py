"""Pinned decisions + decision-log routes — extracted from server.py."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import (
    _authentication_required,
    _db,
    _get_tenant_from_request,
    _hosted_mode,
    validate_input_size,
)
from .. import db as db_module
from ..outbound_url_guard import OutboundURLRejected, avalidate_outbound_url

router = APIRouter()
_log = logging.getLogger(__name__)

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"


def _normalize_consolidated(raw: Any) -> list[dict[str, str]]:
    """Keep only the three fields the preview / replace-all flow uses, as strings.

    The upstream is a caller-chosen host, so its JSON is untrusted: anything
    that is not a list of ``{title, category, body}`` objects is rejected, and
    unexpected keys are dropped rather than echoed back to the caller.
    """
    if not isinstance(raw, list):
        raise ValueError("unexpected upstream shape")
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title, category, text = item.get("title"), item.get("category"), item.get("body")
        out.append({
            "title": title[:500] if isinstance(title, str) else "",
            "category": category[:32] if isinstance(category, str) and category else "TECHNICAL",
            "body": text[:100_000] if isinstance(text, str) else "",
        })
    return out


@router.get("/projects/{project_id}/decisions-pinned")
async def list_pinned_decisions_endpoint(
    project_id: str, request: Request, include_superseded: bool = False
) -> list[dict[str, Any]]:
    """Active pinned decisions (newest first). ``?include_superseded=true`` returns full history."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_pinned_decisions(
        await _db(request), project_id, include_superseded=include_superseded
    )


@router.post("/projects/{project_id}/decisions-pinned", status_code=201)
async def create_pinned_decision_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create a new pinned decision."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    title = (body.get("title") or "").strip()
    text = (body.get("body") or "").strip()
    category = body.get("category", "TECHNICAL")
    priority = body.get("priority", "normal")
    if not title or not text:
        raise HTTPException(status_code=400, detail="title and body required")
    validate_input_size(title, "decision title", 500)
    validate_input_size(text, "decision body", 100_000)
    # G4.15 — safety limit
    from .. import limits as _limits  # noqa: PLC0415
    existing = await db_module.get_pinned_decisions(await _db(request), project_id)
    _limits.check_decisions_per_project(len(existing))
    try:
        result = await db_module.pin_decision(
            await _db(request), project_id, title, text, category, priority=priority
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        from meridian.server import _append_decision_to_md  # noqa: PLC0415

        await _append_decision_to_md(title, text, category)
    except Exception:  # noqa: BLE001
        pass
    return result


@router.patch("/projects/{project_id}/decisions-pinned/{decision_id}")
async def update_pinned_decision_endpoint(
    project_id: str, decision_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch fields, or supersede (pass new_title + new_body to atomically retire+create)."""
    db = await _db(request)
    new_title = body.get("new_title")
    new_body = body.get("new_body")
    if new_title is not None:
        validate_input_size(new_title, "decision title", 500)
    if new_body is not None:
        validate_input_size(new_body, "decision body", 100_000)
    if body.get("title") is not None:
        validate_input_size(body.get("title"), "decision title", 500)
    if body.get("body") is not None:
        validate_input_size(body.get("body"), "decision body", 100_000)
    if new_title and new_body:
        try:
            return await db_module.supersede_pinned_decision(
                db, decision_id, new_title, new_body, body.get("category"),
                priority=body.get("priority"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    result = await db_module.update_pinned_decision(
        db, decision_id,
        body=body.get("body"), title=body.get("title"),
        category=body.get("category"), status=body.get("status"),
        superseded_by=body.get("superseded_by"),
        priority=body.get("priority"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return result


@router.delete("/projects/{project_id}/decisions-pinned/{decision_id}", status_code=204)
async def delete_pinned_decision_endpoint(
    project_id: str, decision_id: str, request: Request
) -> None:
    """Hard-delete a pinned decision. Use update (status=superseded) to archive instead."""
    deleted = await db_module.delete_pinned_decision(await _db(request), decision_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="decision not found")


@router.post("/projects/{project_id}/decisions-pinned/replace-all", status_code=201)
async def replace_all_pinned_decisions(
    project_id: str, body: dict[str, Any], request: Request
) -> list[dict[str, Any]]:
    """Atomically replace all active pinned decisions with a new set (AI consolidation)."""
    decisions = body.get("decisions", [])
    if not decisions:
        raise HTTPException(status_code=400, detail="decisions list required")
    db = await _db(request)
    existing = await db_module.get_pinned_decisions(db, project_id)
    for d in existing:
        await db_module.update_pinned_decision(db, d["id"], status="superseded")
    created = []
    for dec in decisions:
        cat = dec.get("category", "TECHNICAL")
        try:
            row = await db_module.pin_decision(
                db, project_id, title=dec.get("title", "Decision"),
                body=dec.get("body", ""), category=cat,
            )
        except ValueError:
            row = await db_module.pin_decision(
                db, project_id, dec.get("title", "Decision"), dec.get("body", ""), "TECHNICAL"
            )
        created.append(row)
    return created


@router.post("/projects/{project_id}/decisions-pinned/archive-oldest")
async def archive_oldest_pinned_decisions(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Archive the oldest active pinned decisions without creating replacements."""
    raw_count = body.get("count", 1)
    try:
        count = max(1, int(raw_count))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="count must be an integer") from None
    db = await _db(request)
    decisions = await db_module.get_pinned_decisions(db, project_id)
    if not decisions:
        return {"archived": 0}
    to_archive = sorted(
        decisions,
        key=lambda d: ((d.get("created_at") or ""), (d.get("id") or "")),
    )[:count]
    for decision in to_archive:
        await db_module.update_pinned_decision(db, decision["id"], status="superseded")
    return {"archived": len(to_archive)}


@router.post("/projects/{project_id}/decisions/consolidate")
async def consolidate_decisions_ai(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Call an external LLM to deduplicate and consolidate pinned decisions.

    Returns a preview ``{consolidated: [...]}`` for review before applying via replace-all.

    ``base_url`` (optional, OpenAI-compatible models only) is validated by
    :mod:`meridian.outbound_url_guard`: hosted mode accepts only https URLs that
    resolve to public addresses; self-hosted mode also accepts a loopback model
    server (``MERIDIAN_OUTBOUND_ALLOW_PRIVATE=1`` additionally permits a private
    LAN host). Redirects are never followed and upstream bodies are never echoed.
    """
    import json as _json
    import httpx as _httpx

    hosted = _hosted_mode()
    if hosted:
        # Defence in depth: this route makes an outbound request with a
        # caller-supplied key and URL, so it must never depend solely on the
        # app-wide gate / demo read-only middleware. Require a resolved tenant
        # (a forged demo cookie resolves to none).
        tenant = await _get_tenant_from_request(request)
        if tenant is None or not tenant.get("id"):
            raise _authentication_required()

    raw_key = body.get("api_key")
    model = body.get("model") or "claude-haiku-4-5-20251001"
    raw_base_url = body.get("base_url")
    if raw_key is not None and not isinstance(raw_key, str):
        raise HTTPException(status_code=400, detail="api_key must be a string")
    api_key = (raw_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key required")
    if len(api_key) > 4096 or not api_key.isascii() or not api_key.isprintable():
        raise HTTPException(status_code=400, detail="api_key contains invalid characters")
    if not isinstance(model, str):
        raise HTTPException(status_code=400, detail="model must be a string")
    if raw_base_url is not None and not isinstance(raw_base_url, str):
        raise HTTPException(status_code=400, detail="base_url must be a string")
    db = await _db(request)
    if await db_module.get_project(db, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    decisions = await db_module.get_pinned_decisions(db, project_id)
    if not decisions:
        raise HTTPException(status_code=400, detail="no pinned decisions to consolidate")

    decisions_text = "\n\n".join(
        f"[{d.get('category', 'TECHNICAL')}] {d.get('title', '')}\n{d.get('body', '')}"
        for d in decisions
    )
    prompt = (
        "You are a technical decision consolidator. The following are pinned architectural decisions "
        "for a software project. Some may be duplicates or overlap.\n\n"
        "Your task:\n"
        "1. Deduplicate: merge decisions that cover the same topic into one\n"
        "2. Keep all genuinely distinct decisions intact\n"
        "3. Preserve category labels (STRATEGIC, TECHNICAL, PRODUCT, BUSINESS, COMPETITIVE, ARCHITECTURAL, TACTICAL)\n"
        "4. Keep each decision concise (1-3 paragraphs max)\n\n"
        'Return ONLY valid JSON in this exact format, no other text:\n'
        '{"decisions": [{"title": "...", "category": "TECHNICAL", "body": "..."}]}\n\n'
        f"Decisions to consolidate:\n{decisions_text}"
    )
    base_url = _DEFAULT_OPENAI_BASE_URL
    if raw_base_url and not model.startswith("claude"):
        try:
            base_url = await avalidate_outbound_url(raw_base_url, hosted=hosted)
        except OutboundURLRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    # Never follow redirects (a 3xx must not steer the request, or the caller's
    # key, to a host that was not validated); in hosted mode also ignore
    # environment proxies so the connection goes to the validated host.
    client_kwargs: dict[str, Any] = {"timeout": 60.0, "follow_redirects": False}
    if hosted:
        client_kwargs["trust_env"] = False
    try:
        async with _httpx.AsyncClient(**client_kwargs) as client:
            if model.startswith("claude"):
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={"model": model, "max_tokens": 4096,
                          "messages": [{"role": "user", "content": prompt}]},
                )
                r.raise_for_status()
                text = r.json()["content"][0]["text"]
            else:
                r = await client.post(
                    f"{base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                )
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]

        if "```" in text:
            parts = text.split("```")
            text = parts[1][4:] if parts[1].startswith("json") else parts[1]
        consolidated = _normalize_consolidated(_json.loads(text.strip()).get("decisions", []))
        return {"consolidated": consolidated, "original_count": len(decisions)}
    except _httpx.HTTPStatusError as exc:
        # Never reflect the upstream body, URL or exception text: with a
        # caller-chosen host it would be a read channel into whatever answered.
        _log.warning("consolidate: upstream returned HTTP %s", exc.response.status_code)
        raise HTTPException(
            status_code=502, detail=f"AI API error {exc.response.status_code}"
        ) from None
    except Exception as exc:  # noqa: BLE001
        _log.warning("consolidate: upstream request failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502, detail=f"AI API request failed ({type(exc).__name__})"
        ) from None
