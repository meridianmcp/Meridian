"""Pinned decisions + decision-log routes — extracted from server.py."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db
from .. import db as db_module

router = APIRouter()


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
    if not title or not text:
        raise HTTPException(status_code=400, detail="title and body required")
    try:
        return await db_module.pin_decision(await _db(request), project_id, title, text, category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/projects/{project_id}/decisions-pinned/{decision_id}")
async def update_pinned_decision_endpoint(
    project_id: str, decision_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch fields, or supersede (pass new_title + new_body to atomically retire+create)."""
    db = await _db(request)
    new_title = body.get("new_title")
    new_body = body.get("new_body")
    if new_title and new_body:
        try:
            return await db_module.supersede_pinned_decision(
                db, decision_id, new_title, new_body, body.get("category")
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    result = await db_module.update_pinned_decision(
        db, decision_id,
        body=body.get("body"), title=body.get("title"),
        category=body.get("category"), status=body.get("status"),
        superseded_by=body.get("superseded_by"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return result


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
    """
    import json as _json
    import httpx as _httpx

    api_key = (body.get("api_key") or "").strip()
    model = body.get("model") or "claude-haiku-4-5-20251001"
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key required")
    db = await _db(request)
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
    try:
        async with _httpx.AsyncClient(timeout=60.0) as client:
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
                base_url = (body.get("base_url") or "https://api.openai.com").rstrip("/")
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
        consolidated = _json.loads(text.strip()).get("decisions", [])
        return {"consolidated": consolidated, "original_count": len(decisions)}
    except _httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"AI API error {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI API error: {exc}") from exc
