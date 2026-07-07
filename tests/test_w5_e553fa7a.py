"""e553fa7a — editable blog drafts: lock the save_blog_post upsert contract the
new dashboard "Edit" affordance relies on.

The frontend Blog-tab fix (dashboard-blog.ts + loadBlogTab) repopulates the
editor form from a saved draft and re-POSTs to /workspace/blog with the post's
``id``. That path must UPDATE IN PLACE — same id, new title/body/status, and no
duplicate row. save_blog_post already upserts by ``post_id`` (no schema change
was made); these tests assert that behavior explicitly so it can't silently
regress under the UI.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


@pytest.mark.asyncio
async def test_save_blog_post_updates_title_and_body_in_place(db):
    """Re-saving with post_id changes title AND body while keeping the id."""
    created = await db_module.save_blog_post(
        db, "Original Draft", "original body", status="draft", tenant_id="t-1",
    )
    pid = created["id"]
    assert created["status"] == "draft"

    updated = await db_module.save_blog_post(
        db,
        "Edited Title",
        "edited body text",
        status="draft",
        post_id=pid,
        tenant_id="t-1",
    )

    # Same row, new content — this is the edit-in-place the UI drives.
    assert updated["id"] == pid
    assert updated["title"] == "Edited Title"
    assert updated["body_md"] == "edited body text"

    # And it did NOT create a second post.
    posts = await db_module.get_blog_posts(db, tenant_id="t-1")
    assert len(posts) == 1
    assert posts[0]["id"] == pid
    assert posts[0]["title"] == "Edited Title"
    assert posts[0]["body_md"] == "edited body text"


@pytest.mark.asyncio
async def test_save_blog_post_edit_can_publish_a_draft(db):
    """Editing a draft can flip it to published and stamp published_at once."""
    draft = await db_module.save_blog_post(
        db, "Publish Me", "body", status="draft", tenant_id="t-2",
    )
    assert draft["published_at"] is None

    published = await db_module.save_blog_post(
        db, "Publish Me", "body", status="published",
        post_id=draft["id"], tenant_id="t-2",
    )
    assert published["id"] == draft["id"]
    assert published["status"] == "published"
    assert published["published_at"] is not None

    # A later edit keeps the original published_at (COALESCE, not overwrite).
    stamp = published["published_at"]
    re_edited = await db_module.save_blog_post(
        db, "Publish Me v2", "body2", status="published",
        post_id=draft["id"], tenant_id="t-2",
    )
    assert re_edited["published_at"] == stamp
    assert re_edited["title"] == "Publish Me v2"


@pytest.mark.asyncio
async def test_save_blog_post_unknown_id_raises(db):
    """Editing a non-existent post is an error, never a silent insert."""
    with pytest.raises(ValueError):
        await db_module.save_blog_post(
            db, "Ghost", "x", post_id="does-not-exist", tenant_id="t-3",
        )
    # Nothing was created.
    assert await db_module.get_blog_posts(db, tenant_id="t-3") == []
