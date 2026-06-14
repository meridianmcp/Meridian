"""Blog CMS routes (6234f9b8) — admin-authored posts + public serving.

Admin endpoints (``/admin/blog/*``) are gated to admin users and back the
dashboard's admin-only Blog tab. Publishing flips a DB status flag and the post
becomes readable at ``/blog/{slug}`` immediately — no external static-site
pipeline required.

DEFERRED (documented, not built here): committing published posts to a repo +
``git push`` to trigger an Astro/GitHub-Pages build, ``git filter-repo`` on
unpublish, and the ``blog.usemeridian.us`` CNAME. Running git history rewrites
from a hosted endpoint is unsafe and the static-site/DNS pieces are manual infra.
Posts live in the control-plane DB and are served directly.
"""
from __future__ import annotations

import html as _html
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .._deps import _hosted_mode
from .. import db as db_module

router = APIRouter()


async def _require_admin(request: Request) -> None:
    """Allow self-host (single-user) freely; in hosted mode require an admin."""
    if not _hosted_mode():
        return
    from ..hosted import get_current_tenant, is_admin_db  # noqa: PLC0415
    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(status_code=403, detail="not authenticated")
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        raise HTTPException(status_code=403, detail="admin only")


def _render_markdown(md: str) -> str:
    """Minimal, safe markdown -> HTML. Escapes first, then applies a small set
    of conversions (headings, fenced code, inline code, bold, paragraphs). Posts
    are admin-authored, but we still escape to avoid accidental HTML injection."""
    import re
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_code = False
    para: list[str] = []

    def _flush_para() -> None:
        if para:
            text = " ".join(para).strip()
            if text:
                out.append(f"<p>{_inline(text)}</p>")
            para.clear()

    def _inline(text: str) -> str:
        text = _html.escape(text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        return text

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                _flush_para()
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(_html.escape(line))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            _flush_para()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2).strip())}</h{lvl}>")
            continue
        if not line.strip():
            _flush_para()
            continue
        para.append(line.strip())
    if in_code:
        out.append("</code></pre>")
    _flush_para()
    return "\n".join(out)


# --- Admin endpoints -------------------------------------------------------

@router.get("/admin/blog/posts")
async def list_blog_posts_admin(request: Request) -> list[dict[str, Any]]:
    await _require_admin(request)
    return await db_module.list_blog_posts(request.app.state.db)


@router.post("/admin/blog/posts")
async def upsert_blog_post_admin(body: dict[str, Any], request: Request) -> dict[str, Any]:
    await _require_admin(request)
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    return await db_module.upsert_blog_post(
        request.app.state.db,
        post_id=body.get("id"),
        title=title,
        body_md=body.get("body_md") or "",
        slug=body.get("slug"),
    )


@router.get("/admin/blog/posts/{post_id}")
async def get_blog_post_admin(post_id: str, request: Request) -> dict[str, Any]:
    await _require_admin(request)
    post = await db_module.get_blog_post(request.app.state.db, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    return post


@router.post("/admin/blog/posts/{post_id}/publish")
async def publish_blog_post_admin(post_id: str, request: Request) -> dict[str, Any]:
    await _require_admin(request)
    post = await db_module.publish_blog_post(request.app.state.db, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    return post


@router.post("/admin/blog/posts/{post_id}/unpublish")
async def unpublish_blog_post_admin(post_id: str, request: Request) -> dict[str, Any]:
    await _require_admin(request)
    post = await db_module.unpublish_blog_post(request.app.state.db, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    return post


@router.delete("/admin/blog/posts/{post_id}")
async def delete_blog_post_admin(post_id: str, request: Request) -> dict[str, Any]:
    await _require_admin(request)
    ok = await db_module.delete_blog_post(request.app.state.db, post_id)
    if not ok:
        raise HTTPException(status_code=404, detail="post not found")
    return {"deleted": True}


@router.post("/admin/blog/generate-draft")
async def generate_blog_draft(request: Request) -> dict[str, Any]:
    """Build a draft from recent shipped activity (no AI call). Returns
    ``{title, body_md}`` for the editor — does NOT persist."""
    await _require_admin(request)
    db = request.app.state.db
    rows: list[Any] = []
    try:
        async with db.execute(
            "SELECT description FROM task_log WHERE status = 'done' "
            "ORDER BY created_at DESC LIMIT 15"
        ) as cur:
            rows = await cur.fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    bullets = []
    for r in rows:
        desc = (r["description"] if hasattr(r, "__getitem__") else r[0]) or ""
        desc = desc.strip()
        if desc:
            bullets.append(f"- {desc[:200]}")
    body = (
        "Here's what shipped recently:\n\n"
        + ("\n".join(bullets) if bullets else "- (no recent activity found)")
        + "\n\n_Draft generated from recent activity — edit before publishing._"
    )
    return {"title": "What shipped recently", "body_md": body}


# --- Public endpoints ------------------------------------------------------

_BLOG_CSS = (
    "body{font-family:system-ui,sans-serif;background:#0d0d0d;color:#e8e8e8;"
    "max-width:720px;margin:0 auto;padding:40px 20px;line-height:1.6}"
    "a{color:#7dd3fc}h1,h2,h3{color:#fff;line-height:1.25}"
    "code{background:#1a1a1a;padding:1px 5px;border-radius:4px;font-size:.9em}"
    "pre{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:8px;padding:14px;overflow:auto}"
    "pre code{background:none;padding:0}.muted{color:#888;font-size:.85em}"
)


@router.get("/blog", response_class=HTMLResponse)
async def public_blog_index(request: Request) -> HTMLResponse:
    posts = await db_module.list_blog_posts(request.app.state.db, status="published")
    items = "\n".join(
        f'<li><a href="/blog/{_html.escape(p["slug"])}">{_html.escape(p["title"])}</a>'
        f'<div class="muted">{_html.escape((p.get("published_at") or "")[:10])}</div></li>'
        for p in posts
    ) or "<li class='muted'>No posts yet.</li>"
    return HTMLResponse(
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Meridian Blog</title>"
        f"<style>{_BLOG_CSS}</style></head><body><h1>Meridian Blog</h1>"
        f"<ul style='list-style:none;padding:0'>{items}</ul></body></html>"
    )


@router.get("/blog/{slug}", response_class=HTMLResponse)
async def public_blog_post(slug: str, request: Request) -> HTMLResponse:
    post = await db_module.get_blog_post_by_slug(
        request.app.state.db, slug, published_only=True
    )
    if post is None:
        return HTMLResponse(
            f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Not found</title>"
            f"<style>{_BLOG_CSS}</style></head><body><h1>Post not found</h1>"
            f"<p class='muted'><a href='/blog'>← Meridian Blog</a></p></body></html>",
            status_code=404,
        )
    body_html = _render_markdown(post.get("body_md") or "")
    return HTMLResponse(
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{_html.escape(post['title'])} — Meridian Blog</title>"
        f"<style>{_BLOG_CSS}</style></head><body>"
        f"<p class='muted'><a href='/blog'>← Meridian Blog</a></p>"
        f"<h1>{_html.escape(post['title'])}</h1>"
        f"<p class='muted'>{_html.escape((post.get('published_at') or '')[:10])}</p>"
        f"{body_html}</body></html>"
    )
