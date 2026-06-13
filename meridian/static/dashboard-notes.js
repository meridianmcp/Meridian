// dashboard-notes.js — per-project notes wiki tab extracted from dashboard.js
// Re-exposes its symbols on window so inline handlers + cross-file references resolve after esbuild IIFE bundling.

export async function loadNotesTab(projectId) {

  /** v0.9 — load + render the per-project notes wiki.

   *

   * Plain list (no thread / no goal hierarchy). Title, body, tag pills,

   * delete button per note. Adds wire to the [+ Add] button at the

   * bottom of the drawer. Tag filter reloads with `?tag=` substring.

   */

  const body = document.getElementById(`notes-body-${projectId}`);

  const tagFilter = document.getElementById(`notes-tag-${projectId}`);

  const addTitle = document.getElementById(`notes-add-title-${projectId}`);

  const addBody = document.getElementById(`notes-add-body-${projectId}`);

  const addTags = document.getElementById(`notes-add-tags-${projectId}`);

  const addBtn = document.getElementById(`notes-add-btn-${projectId}`);

  if (!body) return;



  const render = async () => {

    body.innerHTML = `<div class="empty" style="color:var(--muted)">loading notes…</div>`;

    const tag = (tagFilter && tagFilter.value || '').trim();

    const qs = tag ? `?tag=${encodeURIComponent(tag)}` : '';

    try {

      const notes = await projectApi(projectId, `/projects/${projectId}/notes${qs}`);

      const visibleNotes = (notes || []).filter(n => {

        const title = String(n.title || '').trim().toLowerCase();

        const tags = String(n.tags || '')

          .split(',')

          .map(t => t.trim().toLowerCase())

          .filter(Boolean);

        return !title.startsWith('checkpoint:') && !tags.includes('checkpoint');

      });

      setVtabCountBadge(`.notes-vtab-badge[data-pid="${projectId}"]`, visibleNotes.length);

      if (!visibleNotes.length) {

        body.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">

          (no notes yet — use the form below or <code>add_note</code> MCP tool)

        </div>`;

        return;

      }

      body.innerHTML = visibleNotes.map(n => {

        const tags = (n.tags || '').split(',').map(t => t.trim()).filter(Boolean);

        const pills = tags.map(t =>

          `<span style="display:inline-block;background:var(--accent)22;color:var(--accent);font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;margin-right:4px">${escapeHtml(t)}</span>`

        ).join('');

        const dt = (n.created_at || '').slice(0, 10);

        return `<div style="background:var(--surface-2);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:0 4px 4px 0;padding:10px 12px;margin-bottom:8px">

          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px">

            <div style="display:flex;align-items:center;gap:8px;min-width:0;flex:1">

              <span style="color:var(--accent);font-weight:600;font-size:12px">${escapeHtml(n.title || '')}</span>

              <span style="color:var(--muted);font-size:10px">${escapeHtml(dt)}</span>

            </div>

            <button class="secondary notes-del-btn" data-note-id="${escapeHtml(n.id)}" style="padding:1px 8px;font-size:10px">Delete</button>

          </div>

          <div style="margin-bottom:6px">${pills}</div>

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

            render();

          } catch (e) { toast('delete failed: ' + e.message, true); }

        };

      });

    } catch (e) {

      body.innerHTML = renderProjectLoadError(projectId, 'Notes unavailable', `/projects/${projectId}/notes${qs}`, e);

      wireProjectLoadRetry(body, projectId);

    }

  };



  if (tagFilter) {

    let t = null;

    tagFilter.oninput = () => { clearTimeout(t); t = setTimeout(render, 250); };

  }

  if (addBtn) addBtn.onclick = async () => {

    const title = (addTitle && addTitle.value || '').trim();

    const text = (addBody && addBody.value || '').trim();

    const tags = (addTags && addTags.value || '').trim();

    if (!title || !text) { toast('title and body required', true); return; }

    try {

      await api(`/projects/${projectId}/notes`, {

        method: 'POST',

        body: JSON.stringify({ title, body: text, tags: tags || undefined }),

      });

      if (addTitle) addTitle.value = '';

      if (addBody) addBody.value = '';

      if (addTags) addTags.value = '';

      toast('note added');

      render();

    } catch (e) { toast('add failed: ' + e.message, true); }

  };



  render();

}

// --- esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { loadNotesTab }); } catch (e) {}
