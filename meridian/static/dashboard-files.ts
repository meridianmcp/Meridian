// dashboard-files.js — file browser + editor tab extracted from dashboard.js
// Re-exposes its symbols on window so inline handlers + cross-file references resolve after esbuild IIFE bundling.

export function _rewriteRepoImages(container: any, projectId: string) {

  /** G7.32 — rewrite repo-relative <img src> in a markdown preview to route

   * through /projects/{pid}/repo-image, which uses the tenant's PAT to

   * fetch raw.githubusercontent.com. Absolute URLs and data URIs pass

   * through unchanged. Limits noted in the server endpoint docstring. */

  if (!container || !projectId) return;

  container.querySelectorAll('img').forEach((img: any) => {

    const src = img.getAttribute('src') || '';

    if (!src) return;

    if (/^https?:\/\//i.test(src) || src.startsWith('data:') || src.startsWith('/')) return;

    const path = src.replace(/^\.\//, '');

    img.setAttribute('src', `/projects/${projectId}/repo-image?path=${encodeURIComponent(path)}`);

    img.setAttribute('loading', 'lazy');

  });

}

export async function loadFilesTab(projectId: string) {

  /**Load the list of editable files from the server and render them as

   * clickable items in the files drawer panel. */

  const listEl = document.getElementById(`files-list-${projectId}`);

  if (!listEl) return;

  try {

    const files = await api(`/projects/${projectId}/files`);

    if (!files || !files.length) {

      listEl.innerHTML = `<div style="padding:14px;color:var(--muted);font-family:'IBM Plex Mono',monospace;font-size:11px">No editable files found.</div>`;

      return;

    }

    listEl.innerHTML = files.map((f: any) =>

      `<div class="file-item" data-filename="${escapeHtml(f)}">${escapeHtml(f)}</div>`

    ).join('');

    listEl.querySelectorAll('.file-item').forEach(item => {

      item.onclick = () => openFileEditor(projectId, item.dataset.filename);

    });

  } catch(e: any) {

    listEl.innerHTML = `<div style="padding:14px;color:var(--status-failed);font-family:'IBM Plex Mono',monospace;font-size:11px">Error: ${escapeHtml(e.message)}</div>`;

  }

}

export async function openFileEditor(projectId: string, filename: string) {

  /**Fetch file content and switch the files panel into editor mode. */

  const browseEl = document.getElementById(`files-browse-${projectId}`);

  const editorEl = document.getElementById(`file-editor-wrap-${projectId}`);

  const nameEl = document.getElementById(`file-name-${projectId}`);

  const contentEl = document.getElementById(`file-content-${projectId}`);

  if (!browseEl || !editorEl || !contentEl || !nameEl) return;

  try {

    const data = await api(`/projects/${projectId}/files/${encodeURIComponent(filename)}`);

    contentEl.value = data.content || '';

    nameEl.textContent = filename;

    browseEl.style.display = 'none';

    editorEl.style.display = 'flex';

    // Default to preview mode when opening a file

    const editBtn = document.getElementById(`file-mode-edit-${projectId}`);

    const previewBtn = document.getElementById(`file-mode-preview-${projectId}`);

    const previewDiv = document.getElementById(`file-preview-${projectId}`);

    if (editBtn) editBtn.classList.remove('active');

    if (previewBtn) previewBtn.classList.add('active');

    // Render markdown immediately into preview

    if (previewDiv) {

      const md = data.content || '';

      const html = (typeof marked !== 'undefined') ? marked.parse(md) : escapeHtml(md);

      previewDiv.innerHTML = html;

      _rewriteRepoImages(previewDiv, projectId);

      previewDiv.style.display = '';

    }

    contentEl.style.display = 'none';

    // Wire edit/preview toggle if not already wired

    if (editBtn && previewBtn && previewDiv && !editBtn._wired) {

      editBtn._wired = true;

      [editBtn, previewBtn].forEach((btn: any) => {

        btn.onclick = () => {

          [editBtn, previewBtn].forEach((b: any) => b.classList.toggle('active', b === btn));

          if (btn.dataset.fmode === 'preview') {

            const md = contentEl.value || '';

            const html = (typeof marked !== 'undefined') ? marked.parse(md) : escapeHtml(md);

            previewDiv.innerHTML = html;

            _rewriteRepoImages(previewDiv, projectId);

            contentEl.style.display = 'none';

            previewDiv.style.display = '';

          } else {

            previewDiv.style.display = 'none';

            contentEl.style.display = '';

          }

        };

      });

    }

  } catch(e: any) { toast('open failed: ' + e.message, true); }

}

export async function saveFile(projectId: string) {

  /**Write the current editor content back to the server. */

  const nameEl = document.getElementById(`file-name-${projectId}`);

  const contentEl = document.getElementById(`file-content-${projectId}`);

  if (!nameEl || !contentEl) return;

  const filename = nameEl.textContent.trim();

  if (!filename) return;

  try {

    await api(`/projects/${projectId}/files/${encodeURIComponent(filename)}`, {

      method: 'PUT',

      body: JSON.stringify({ content: contentEl.value }),

    });

    toast(`saved ${filename}`);

  } catch(e: any) { toast('save failed: ' + e.message, true); }

}

// --- esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { _rewriteRepoImages, loadFilesTab, openFileEditor, saveFile }); } catch (e) {}
