// dashboard-notes.js — per-project notes wiki tab extracted from dashboard.js
// Re-exposes its symbols on window so inline handlers + cross-file references resolve after esbuild IIFE bundling.

export async function loadNotesTab(projectId: string) {
  /** v0.9 / e5592013 — load + render the per-project notes wiki.
   *
   * Plain list (no thread / no goal hierarchy). Title, body, tag pills,
   * delete button per note. Client-side controls: a free-text search box
   * (matches title/body/tags), a tag-filter dropdown populated from the
   * tags actually present, and a "summaries" toggle that reveals the
   * auto-captured checkpoint/session-summary notes (hidden by default so
   * the wiki isn't drowned out by them).
   */
  const body = document.getElementById(`notes-body-${projectId}`);
  const searchInput = document.getElementById(`notes-search-${projectId}`);
  const tagSelect = document.getElementById(`notes-tagsel-${projectId}`);
  const kindSelect = document.getElementById(`notes-kindsel-${projectId}`);
  const showAuto = document.getElementById(`notes-show-auto-${projectId}`);

  // 9d44998b — note_kind taxonomy. NULL/unknown renders as the compact "wiki".
  const KIND_STYLE: Record<string, { label: string; color: string; border: string }> = {
    wiki: { label: 'wiki', color: 'var(--muted)', border: 'var(--border)' },
    insight: { label: 'insight', color: 'var(--accent)', border: 'var(--accent)' },
    reference: { label: 'reference', color: '#c9a227', border: '#c9a227' },
  };
  const noteKind = (n: any) => {
    const k = String(n.note_kind || '').toLowerCase();
    return KIND_STYLE[k] ? k : 'wiki';
  };
  const addTitle = document.getElementById(`notes-add-title-${projectId}`);
  const addBody = document.getElementById(`notes-add-body-${projectId}`);
  const addTags = document.getElementById(`notes-add-tags-${projectId}`);
  const addKind = document.getElementById(`notes-add-kind-${projectId}`);
  const addBtn = document.getElementById(`notes-add-btn-${projectId}`);
  if (!body) return;

  // Auto-captured session summaries: auto_capture_session() writes notes titled
  // "Session summary (date)" tagged "auto-capture"; the legacy path wrote
  // "checkpoint: …" / tag "checkpoint". Hidden unless the toggle is on.
  const isAutoCapture = (n: any) => {
    const title = String(n.title || '').trim().toLowerCase();
    const tags = String(n.tags || '').split(',').map(t => t.trim().toLowerCase());
    return title.startsWith('checkpoint:')
      || title.startsWith('session summary')
      || tags.includes('checkpoint')
      || tags.includes('auto-capture');
  };

  const noteTags = (n: any) =>
    String(n.tags || '').split(',').map(t => t.trim()).filter(Boolean);

  let allNotes: any[] = [];
  // 9fa119dd — cursor pagination: the notes list caps at NOTES_PAGE per fetch
  // and a "Load More" button pulls the next cursor and appends, mirroring the
  // devlog/tasks Load-More wiring. allNotes accumulates every loaded page so the
  // client-side search / tag / kind filters keep working over what's loaded.
  const NOTES_PAGE = 100;
  let nextCursor = 0;
  let hasMore = false;

  // Render (or remove) the "Load More" button below the notes list based on the
  // current hasMore flag — appended after the filtered list each render.
  const renderLoadMore = () => {
    const existing = document.getElementById(`notes-load-more-${projectId}`);
    if (existing) existing.remove();
    if (!hasMore) return;
    const btn = document.createElement('button');
    btn.id = `notes-load-more-${projectId}`;
    btn.className = 'secondary';
    btn.style = 'width:100%;margin-top:8px;padding:5px;font-size:11px;font-family:var(--font-mono)';
    btn.textContent = `Load ${NOTES_PAGE} more ↓`;
    btn.onclick = () => loadMore(btn);
    body.appendChild(btn);
  };

  // Rebuild the tag dropdown from whatever tags are present, preserving the
  // current selection if it still exists.
  const refreshTagOptions = () => {
    if (!tagSelect) return;
    const prev = tagSelect.value;
    const seen = new Set<string>();
    for (const n of allNotes) {
      if (!showAuto?.checked && isAutoCapture(n)) continue;
      for (const t of noteTags(n)) seen.add(t);
    }
    const tags = [...seen].sort((a, b) => a.localeCompare(b));
    tagSelect.innerHTML =
      `<option value="">all tags</option>` +
      tags.map(t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join('');
    if (tags.includes(prev)) tagSelect.value = prev;
  };

  const applyFilters = () => {
    const q = (searchInput?.value || '').trim().toLowerCase();
    const selectedTag = (tagSelect?.value || '').trim().toLowerCase();
    const selectedKind = (kindSelect?.value || '').trim().toLowerCase();
    const includeAuto = !!showAuto?.checked;

    const visible = allNotes.filter(n => {
      if (!includeAuto && isAutoCapture(n)) return false;
      if (selectedKind && noteKind(n) !== selectedKind) return false;
      if (selectedTag && !noteTags(n).map(t => t.toLowerCase()).includes(selectedTag)) return false;
      if (q) {
        const hay = `${n.title || ''}\n${n.body || ''}\n${n.tags || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    setVtabCountBadge(`.notes-vtab-badge[data-pid="${projectId}"]`,
      allNotes.filter(n => !isAutoCapture(n)).length);

    if (!visible.length) {
      const reason = allNotes.length
        ? `(no notes match — clear the search/tag filter${!includeAuto ? ' or tick “summaries”' : ''})`
        : `(no notes yet — use the form below or <code>add_note</code> MCP tool)`;
      body.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">${reason}</div>`;
      renderLoadMore();
      return;
    }

    body.innerHTML = visible.map(n => {
      const pills = noteTags(n).map(t =>
        `<span style="display:inline-block;background:var(--accent)22;color:var(--accent);font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;margin-right:4px">${escapeHtml(t)}</span>`
      ).join('');
      const dt = (n.created_at || '').slice(0, 10);
      const kind = noteKind(n);
      const ks = KIND_STYLE[kind];
      // insights get more weight (thicker accent rail + larger title); wiki/reference compact.
      const isInsight = kind === 'insight';
      const kindPill = `<span title="note kind: ${ks.label}" style="display:inline-block;background:${ks.color}22;color:${ks.color};font-size:9px;font-weight:700;padding:1px 6px;border-radius:3px;margin-right:4px;text-transform:uppercase;letter-spacing:0.4px">${ks.label}</span>`;
      const autoPill = isAutoCapture(n)
        ? `<span title="Auto-captured session summary" style="display:inline-block;background:var(--surface-3,#2a2f3a);color:var(--muted);font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;margin-right:4px">session</span>`
        : '';
      return `<div style="background:var(--surface-2);border:1px solid var(--border);border-left:${isInsight ? '4px' : '3px'} solid ${ks.border};border-radius:0 4px 4px 0;padding:${isInsight ? '12px 14px' : '10px 12px'};margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px">
            <div style="display:flex;align-items:center;gap:8px;min-width:0;flex:1">
              <span style="color:var(--accent);font-weight:600;font-size:${isInsight ? '13px' : '12px'}">${escapeHtml(n.title || '')}</span>
              <span style="color:var(--muted);font-size:10px">${escapeHtml(dt)}</span>
            </div>
            <button class="secondary notes-del-btn guest-hidden" data-note-id="${escapeHtml(n.id)}" style="padding:1px 8px;font-size:10px">Delete</button>
          </div>
          <div style="margin-bottom:6px">${kindPill}${autoPill}${pills}</div>
          <div class="note-body-md" style="color:var(--text);line-height:1.5;font-size:12px">${typeof marked !== 'undefined' ? marked.parse(n.body || '') : escapeHtml(n.body || '')}</div>
        </div>`;
    }).join('');

    body.querySelectorAll('.notes-del-btn').forEach(btn => {
      btn.onclick = async () => {
        if (!confirm('Delete this note?')) return;
        try {
          const r = await fetch(`/projects/${projectId}/notes/${btn.dataset.noteId}`, { method: 'DELETE' });
          if (!r.ok) throw new Error(`${r.status}`);
          toast('note deleted');
          await load();
        } catch (e: any) { toast('delete failed: ' + e.message, true); }
      };
    });

    renderLoadMore();
  };

  // Fetch the next cursor page and append it to allNotes, then re-render. The
  // server returns {notes, has_more, next_cursor}; cursor is the next offset.
  const loadMore = async (btn: any) => {
    if (btn) { btn.disabled = true; btn.textContent = 'loading…'; }
    try {
      const page = await projectApi(
        projectId,
        `/projects/${projectId}/notes?paginate=true&limit=${NOTES_PAGE}&cursor=${nextCursor}`,
      ) || {};
      allNotes = [...allNotes, ...(page.notes || [])];
      hasMore = !!page.has_more;
      nextCursor = page.next_cursor != null ? page.next_cursor : nextCursor;
      refreshTagOptions();
      applyFilters();
    } catch (e: any) {
      if (btn) { btn.disabled = false; btn.textContent = `Load ${NOTES_PAGE} more ↓ (retry)`; }
      toast('load more failed: ' + e.message, true);
    }
  };

  const load = async () => {
    body.innerHTML = `<div class="empty" style="color:var(--muted)">loading notes…</div>`;
    try {
      // 9fa119dd — first cursor page. ?paginate=true returns the
      // {notes, has_more, next_cursor} envelope (vs. the legacy bare list).
      const page = await projectApi(
        projectId,
        `/projects/${projectId}/notes?paginate=true&limit=${NOTES_PAGE}&cursor=0`,
      ) || {};
      allNotes = page.notes || [];
      hasMore = !!page.has_more;
      nextCursor = page.next_cursor != null ? page.next_cursor : 0;
      refreshTagOptions();
      applyFilters();
    } catch (e) {
      body.innerHTML = renderProjectLoadError(projectId, 'Notes unavailable', `/projects/${projectId}/notes`, e);
      wireProjectLoadRetry(body, projectId);
    }
  };

  if (searchInput) {
    let t: any = null;
    searchInput.oninput = () => { clearTimeout(t); t = setTimeout(applyFilters, 150); };
  }
  if (tagSelect) tagSelect.onchange = applyFilters;
  if (kindSelect) kindSelect.onchange = applyFilters;
  if (showAuto) showAuto.onchange = () => { refreshTagOptions(); applyFilters(); };

  if (addBtn) addBtn.onclick = async () => {
    const title = (addTitle && addTitle.value || '').trim();
    const text = (addBody && addBody.value || '').trim();
    const tags = (addTags && addTags.value || '').trim();
    if (!title || !text) { toast('title and body required', true); return; }
    if (title.length > 500) { toast('Title too long (500 char limit)', true); if (addTitle) addTitle.style.borderColor = 'var(--red, #f87171)'; return; }
    if (addTitle) addTitle.style.borderColor = '';
    try {
      const res = await api(`/projects/${projectId}/notes`, {
        method: 'POST',
        body: JSON.stringify({ title, body: text, tags: tags || undefined, kind: (addKind && addKind.value) || undefined }),
      });
      if (addTitle) addTitle.value = '';
      if (addBody) addBody.value = '';
      if (addTags) addTags.value = '';
      // e5592013 — surface the MANUAL-note lint hint from the server.
      if (res && res.lint) toast(res.lint, false);
      else toast('note added');
      await load();
    } catch (e: any) { toast('add failed: ' + e.message, true); }
  };

  await load();
}

// --- esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { loadNotesTab }); } catch (e) {}
