// dashboard-blog.ts (e553fa7a) — pure HTML builders for the workspace Blog tab.
// Extracted from dashboard.ts's loadBlogTab so the "Edit repopulates the form"
// behavior is unit-testable in isolation (vitest). Depends on the global
// escapeHtml (dashboard-utils) at runtime, matching dashboard-mcp.ts.
//
// BUG e553fa7a: drafts (and any saved post) had no UI to edit title/body once
// saved. These builders render an inline editor form plus a per-post "Edit"
// affordance; loadBlogTab wires the Edit button to repopulate the form from the
// post's own fields, then re-saves via POST /workspace/blog with the post id
// (save_blog_post already upserts by id — update in place, no schema change).

export interface BlogPostView {
  id?: string;
  title?: string;
  body_md?: string;
  slug?: string;
  status?: string;
  url?: string;
}

const _BLOG_STATUSES = ['draft', 'published', 'archived'] as const;

/** Editor form (create + edit). When `post` is supplied the fields are
 *  pre-populated from it and a hidden id input carries the post id so a save
 *  updates in place. Field ids are namespaced by `projectId` so multiple open
 *  panels don't collide. */
export function blogEditorFormHtml(projectId: string, post?: BlogPostView | null): string {
  const p = post || {};
  const id = String(p.id || '');
  const title = String(p.title || '');
  const body = String(p.body_md || '');
  const status = _BLOG_STATUSES.includes(String(p.status) as any) ? String(p.status) : 'draft';
  const editing = !!id;
  const pid = escapeHtml(String(projectId));
  const opts = _BLOG_STATUSES
    .map(s => `<option value="${s}"${s === status ? ' selected' : ''}>${s}</option>`)
    .join('');
  return `<div id="blog-editor-${pid}" style="border:1px solid var(--border);border-radius:4px;padding:10px;margin-bottom:14px;background:var(--surface-1)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span id="blog-editor-title-${pid}" style="font-size:10px;color:var(--accent);letter-spacing:.06em">${editing ? 'EDIT POST' : 'NEW POST'}</span>
      <button id="blog-editor-reset-${pid}" class="secondary" style="font-size:9px;padding:2px 8px;${editing ? '' : 'display:none'}">New / clear</button>
    </div>
    <input type="hidden" id="blog-editor-id-${pid}" value="${escapeHtml(id)}" />
    <input type="text" id="blog-editor-title-input-${pid}" placeholder="Title" value="${escapeHtml(title)}" style="width:100%;box-sizing:border-box;font-size:11px;padding:5px 7px;margin-bottom:6px;background:var(--surface-0,var(--bg));color:var(--text);border:1px solid var(--border);border-radius:3px" />
    <textarea id="blog-editor-body-${pid}" placeholder="Body (markdown)" rows="6" style="width:100%;box-sizing:border-box;font-size:11px;font-family:var(--font-mono);padding:5px 7px;margin-bottom:6px;background:var(--surface-0,var(--bg));color:var(--text);border:1px solid var(--border);border-radius:3px;resize:vertical">${escapeHtml(body)}</textarea>
    <div style="display:flex;gap:6px;align-items:center">
      <select id="blog-editor-status-${pid}" style="font-size:10px;padding:3px 6px;background:var(--surface-0,var(--bg));color:var(--text);border:1px solid var(--border);border-radius:3px">${opts}</select>
      <button id="blog-editor-save-${pid}" style="font-size:10px;padding:4px 12px">${editing ? 'Update post' : 'Save post'}</button>
      <span id="blog-editor-status-msg-${pid}" style="font-size:9px;color:var(--muted)"></span>
    </div>
  </div>`;
}

/** Single post card with an Edit affordance. The Edit button carries the post's
 *  fields as data-* attributes so the click handler can repopulate the form
 *  without another fetch. */
export function blogPostCardHtml(post: BlogPostView): string {
  const id = String(post.id || '');
  const slug = String(post.slug || '');
  const url = String(post.url || (slug ? `/blog/${slug}` : ''));
  const title = String(post.title || 'Untitled');
  return `<div style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;margin-bottom:8px;background:var(--surface-1)">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
      <div style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(title)}</div>
      <button class="blog-edit-btn secondary" data-blog-id="${escapeHtml(id)}" style="font-size:9px;padding:2px 8px;flex:0 0 auto">Edit</button>
    </div>
    <div style="font-size:9px;color:var(--muted);font-family:var(--font-mono);margin-top:3px">${escapeHtml(slug)}</div>
    ${url ? `<div style="margin-top:4px"><a href="${escapeHtml(url)}" target="_blank" rel="noopener" style="font-size:10px;color:var(--accent)">${escapeHtml(url)}</a></div>` : ''}
  </div>`;
}

/** Populate the editor form's inputs from a post (used by the Edit button).
 *  Pure DOM mutation, kept here so it can be unit-tested against a jsdom form
 *  built by blogEditorFormHtml. Returns true when the form was found. */
export function populateBlogEditor(projectId: string, post: BlogPostView): boolean {
  const pid = String(projectId);
  const idEl = document.getElementById(`blog-editor-id-${pid}`) as HTMLInputElement | null;
  const titleEl = document.getElementById(`blog-editor-title-input-${pid}`) as HTMLInputElement | null;
  const bodyEl = document.getElementById(`blog-editor-body-${pid}`) as HTMLTextAreaElement | null;
  const statusEl = document.getElementById(`blog-editor-status-${pid}`) as HTMLSelectElement | null;
  const labelEl = document.getElementById(`blog-editor-title-${pid}`);
  const saveEl = document.getElementById(`blog-editor-save-${pid}`);
  const resetEl = document.getElementById(`blog-editor-reset-${pid}`);
  if (!idEl || !titleEl || !bodyEl) return false;
  idEl.value = String(post.id || '');
  titleEl.value = String(post.title || '');
  bodyEl.value = String(post.body_md || '');
  if (statusEl) {
    const st = _BLOG_STATUSES.includes(String(post.status) as any) ? String(post.status) : 'draft';
    statusEl.value = st;
  }
  if (labelEl) labelEl.textContent = 'EDIT POST';
  if (saveEl) saveEl.textContent = 'Update post';
  if (resetEl) (resetEl as HTMLElement).style.display = '';
  return true;
}

/** Reset the editor form back to "new post" state. */
export function resetBlogEditor(projectId: string): void {
  const pid = String(projectId);
  const idEl = document.getElementById(`blog-editor-id-${pid}`) as HTMLInputElement | null;
  const titleEl = document.getElementById(`blog-editor-title-input-${pid}`) as HTMLInputElement | null;
  const bodyEl = document.getElementById(`blog-editor-body-${pid}`) as HTMLTextAreaElement | null;
  const statusEl = document.getElementById(`blog-editor-status-${pid}`) as HTMLSelectElement | null;
  const labelEl = document.getElementById(`blog-editor-title-${pid}`);
  const saveEl = document.getElementById(`blog-editor-save-${pid}`);
  const resetEl = document.getElementById(`blog-editor-reset-${pid}`);
  if (idEl) idEl.value = '';
  if (titleEl) titleEl.value = '';
  if (bodyEl) bodyEl.value = '';
  if (statusEl) statusEl.value = 'draft';
  if (labelEl) labelEl.textContent = 'NEW POST';
  if (saveEl) saveEl.textContent = 'Save post';
  if (resetEl) (resetEl as HTMLElement).style.display = 'none';
}

// --- esbuild IIFE: re-expose builders as globals so cross-file references and
// any inline handlers keep resolving after bundling (matches dashboard-mcp.ts).
try {
  Object.assign(window, {
    blogEditorFormHtml,
    blogPostCardHtml,
    populateBlogEditor,
    resetBlogEditor,
  });
} catch (e) { /* non-browser (test) env */ }
