const TABS_KEY = 'meridian.openTabs';
const ACTIVE_PROJECT_KEY = 'meridian.activeProject';
const state = {
  projects: [],
  tabs: [], // [{id, project}]
  activeTab: null,
  panels: {}, // tabId -> { ws, taskCache, sessionName, chatHistory, goalRaw, goalIsJson }
  apiKeyConfigured: false,
  // v0.6.5 — server runtime config fetched from /config on startup.
  serverConfig: { server_url: '', host: '', port: 0, version: '' },
};

function toast(msg, isError=false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 2600);
}

async function api(path, opts={}) {
  const r = await fetch(path, { headers: {'Content-Type': 'application/json'}, ...opts });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status}: ${text}`);
  }
  return r.status === 204 ? null : r.json();
}

async function loadServerConfig() {
  // v0.6.5 — pull /config so the dashboard can show the version and
  // (in hosted deployments) target a non-localhost server_url.
  try {
    const cfg = await api('/config');
    state.serverConfig = cfg || state.serverConfig;
    const verEl = document.getElementById('server-version');
    if (verEl && cfg?.version) verEl.textContent = `v${cfg.version}`;
    // v2.0-fixes — demo mode banner
    if (cfg?.demo_mode && !document.getElementById('demo-mode-banner')) {
      const b = document.createElement('div');
      b.id = 'demo-mode-banner';
      b.style = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:#7c3aed;color:#fff;text-align:center;padding:5px 12px;font-size:12px;font-family:inherit;letter-spacing:0.02em';
      b.textContent = 'Preview mode — read only';
      document.body.prepend(b);
      document.body.style.paddingTop = ((parseInt(document.body.style.paddingTop || '0', 10)) + 28) + 'px';
    }
    // v1.9.x — update connection indicator
    _updateConnectionIndicator(cfg);
  } catch (e) { /* offline / older server — ignore */ }
}

// v1.9.x — show active DB connection in sidebar footer
function _updateConnectionIndicator(cfg) {
  if (!cfg) return;
  const wrap = document.getElementById('connection-indicator');
  const label = document.getElementById('connection-label');
  const dot = document.getElementById('connection-dot');
  const switcher = document.getElementById('connection-switcher');
  if (!wrap || !label) return;
  wrap.style.display = 'block';
  const name = cfg.connection_name || (cfg.db === 'postgres' ? 'postgres' : 'local');
  const dbType = cfg.db || 'sqlite';
  label.textContent = name + ' (' + dbType + ')';
  dot.style.background = dbType === 'postgres' ? 'var(--accent)' : 'var(--accent-green)';
  // Make indicator clickable to show connection popup
  const conns = cfg.connections || [];
  if (conns.length > 0 && wrap) {
    wrap.style.cursor = 'pointer';
    wrap.title = 'Click to switch connection';
    wrap.onclick = (e) => {
      e.stopPropagation();
      document.querySelectorAll('.conn-popup').forEach(p => p.remove());
      const popup = document.createElement('div');
      popup.className = 'conn-popup';
      popup.style.cssText = 'position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1001;min-width:180px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:11px;font-family:var(--font-mono);padding:6px 0';
      const rect = wrap.getBoundingClientRect();
      popup.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
      popup.style.left = rect.left + 'px';
      // Header
      const hdr = document.createElement('div');
      hdr.style.cssText = 'padding:4px 12px;color:var(--muted);font-size:10px;border-bottom:1px solid var(--border);margin-bottom:4px';
      hdr.textContent = 'Select connection';
      popup.appendChild(hdr);
      // Connection options
      // Always include current env connection if not already in list
      let displayConns = [...(conns || [])];
      const envConn = {name: cfg.connection_name || (cfg.db === 'postgres' ? 'env (postgres)' : 'local'), type: cfg.db, active: true};
      if (!displayConns.find(c => c.active)) displayConns.unshift(envConn);
      displayConns.forEach(c => {
        const item = document.createElement('div');
        item.style.cssText = `padding:6px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;justify-content:space-between;${c.active ? 'color:var(--accent)' : 'color:var(--text)'}`;
        const left = document.createElement('div');
        left.style.cssText = 'display:flex;align-items:center;gap:8px;flex:1;min-width:0';
        const dot2 = document.createElement('span');
        dot2.style.cssText = `display:inline-block;width:6px;height:6px;border-radius:50%;flex-shrink:0;background:${c.active ? 'var(--accent)' : 'var(--muted)'}`;
        left.appendChild(dot2);
        left.appendChild(document.createTextNode(c.name + ' (' + (c.type || 'sqlite') + ')'));
        item.appendChild(left);
        // Delete button (only for non-active named connections)
        if (!c.active && c.name && c.name !== 'local') {
          const del = document.createElement('button');
          del.textContent = '×';
          del.title = 'Remove connection';
          del.style.cssText = 'background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:0 2px;line-height:1;flex-shrink:0';
          del.onmouseenter = () => del.style.color = 'var(--status-failed)';
          del.onmouseleave = () => del.style.color = 'var(--muted)';
          del.onclick = async (e) => {
            e.stopPropagation();
            if (!confirm('Remove connection "' + c.name + '"?')) return;
            try {
              await api('/config/connections/' + encodeURIComponent(c.name), { method: 'DELETE' });
              popup.remove();
              await loadServerConfig();
            } catch(ex) { toast('Remove failed: ' + ex.message, true); }
          };
          item.appendChild(del);
        }
        item.onmouseenter = () => { if (!c.active) left.style.color = 'var(--accent)'; item.style.background = 'var(--surface-3)'; };
        item.onmouseleave = () => { left.style.color = ''; item.style.background = ''; };
        item.onclick = async (e) => {
          if (e.target.tagName === 'BUTTON') return; // don't activate on delete click
          popup.remove();
          try {
            await api('/config/connections', { method: 'POST', body: JSON.stringify({ name: c.name, activate: true }) });
            await loadServerConfig();
            // Show restart banner — DB connection change needs restart
            const banner = document.getElementById('update-banner');
            const bannerSpan = banner?.querySelector('span');
            if (banner) {
              banner.style.display = 'block';
              if (bannerSpan) bannerSpan.textContent = '\u26A0\uFE0F Connection changed to ' + c.name + ' \u2014 restart to apply';
            }
            // Update indicator immediately
            const dot = document.getElementById('connection-dot');
            const label = document.getElementById('connection-label');
            if (dot) dot.style.background = 'var(--accent)';
            if (label) label.textContent = c.name + ' (' + (c.type || 'sqlite') + ')';
          } catch(e) { console.error('Switch failed:', e); toast('Switch failed: ' + e.message, true); }
        };
        popup.appendChild(item);
      });
      const addItem = document.createElement('div');
      addItem.style.cssText = 'padding:6px 12px;cursor:pointer;color:var(--muted);border-top:1px solid var(--border);margin-top:4px';
      addItem.textContent = '+ Add connection...';
      addItem.onmouseenter = () => addItem.style.color = 'var(--text)';
      addItem.onmouseleave = () => addItem.style.color = 'var(--muted)';
      addItem.onclick = () => { popup.remove(); document.getElementById('conn-setup-modal').style.display = 'flex'; };
      popup.appendChild(addItem);
      // Config file path at bottom
      if (cfg.toml_path) {
        const pathRow = document.createElement('div');
        pathRow.style.cssText = 'padding:4px 12px 6px;color:var(--muted);font-size:9px;border-top:1px solid var(--border);margin-top:2px;word-break:break-all';
        pathRow.textContent = '📄 ' + cfg.toml_path;
        popup.appendChild(pathRow);
      }
      document.body.appendChild(popup);
      setTimeout(() => document.addEventListener('click', () => popup.remove(), { once: true }), 0);
    };
  }
  if (conns.length > 1 && switcher) {
    switcher.style.display = 'none'; // replaced by popup
    switcher.innerHTML = conns.map(c =>
      `<option value="${c.name}" ${c.active ? 'selected' : ''}>${c.name}</option>`
    ).join('');
    switcher.onchange = async () => {
      try {
        const sel = switcher.value;
        const conn = (cfg.connections || []).find(c => c.name === sel) || {};
        await api('/config/connections', {
          method: 'POST',
          body: JSON.stringify({ name: sel, type: conn.type || 'sqlite', activate: true }),
        });
        if (conn.type === 'postgres') {
          toast('Switching to ' + sel + ' — restarting…');
          await _doRestart();
        } else {
          toast('Switched to ' + sel + ' — restart to apply');
        }
      } catch(e) { toast('Switch failed: ' + e.message, true); }
    };
  }
}

// v1.9.x — POST /admin/restart then poll /health until up, then reload.
async function checkGitStatus() {
  const btn = document.getElementById('git-check-btn');
  if (btn) { btn.textContent = 'checking…'; btn.style.color = 'var(--muted)'; }
  try {
    const s = await api('/admin/git-status');
    if (!s.ok) throw new Error(s.error || 'git check failed');
    if (s.behind > 0) {
      const banner = document.getElementById('update-banner');
      const span = banner?.querySelector('span');
      if (banner) {
        banner.style.display = 'block';
        if (span) span.textContent = `\u26A0\uFE0F ${s.behind} commit${s.behind>1?'s':''} behind origin/${s.branch} (${s.local_hash} \u2260 ${s.remote_hash}) \u2014 git pull recommended`;
      }
      if (btn) { btn.textContent = `\u2193 ${s.behind} behind`; btn.style.color = 'var(--status-failed)'; }
    } else {
      if (btn) { btn.textContent = '\u2713 up to date'; btn.style.color = 'var(--status-done)'; }
      setTimeout(() => { if (btn) { btn.textContent = 'check updates'; btn.style.color = 'var(--muted)'; } }, 3000);
    }
  } catch(e) {
    if (btn) { btn.textContent = 'check updates'; btn.style.color = 'var(--muted)'; }
  }
}

async function _doRestart() {
  try { await fetch('/admin/restart', { method: 'POST' }); } catch(_) { /* expected */ }
  // Replace any existing restart button text
  document.querySelectorAll('#restart-server-btn, #banner-restart-btn').forEach(b => {
    b.textContent = 'Restarting…'; b.disabled = true;
  });
  const started = Date.now();
  while (Date.now() - started < 30000) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      const r = await fetch('/health');
      if (r.ok) { window.location.href = window.location.pathname + '?_cb=' + Date.now(); return; }
    } catch(_) { /* server still down — keep polling */ }
  }
  // Timed out
  document.querySelectorAll('#restart-server-btn, #banner-restart-btn').forEach(b => {
    b.textContent = 'Restart timed out'; b.disabled = false;
  });
  toast('Server did not come back within 30s — start manually', true);
}

async function loadConfig() {
  try {
    const cfg = await api('/config/api-key');
    state.apiKeyConfigured = !!cfg.configured;
    document.getElementById('api-warn').style.display = cfg.configured ? 'none' : 'block';
    const methodEl = document.getElementById('auth-method');
    if (cfg.method === 'oauth') {
      methodEl.textContent = 'Auth: Claude Max OAuth';
      methodEl.style.display = 'block';
    } else if (cfg.method === 'api_key') {
      methodEl.textContent = 'Auth: API key';
      methodEl.style.display = 'block';
    } else {
      methodEl.style.display = 'none';
    }
  } catch (e) { /* ignore */ }
}

async function loadProjects() {
  state.projects = await api('/projects');
  const list = document.getElementById('project-list');
  list.innerHTML = '';
  state.projects.forEach(p => {
    const div = document.createElement('div');
    div.className = 'project-item';
    div.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:4px';
    const nameSpan = document.createElement('span');
    nameSpan.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    nameSpan.textContent = p.name;
    // id shown in kebab menu instead
    const menuBtn = document.createElement('button');
    menuBtn.textContent = '⋯';
    menuBtn.title = 'Project actions';
    menuBtn.style.cssText = 'background:none;border:none;color:var(--muted);cursor:pointer;padding:0 4px;font-size:14px;line-height:1;flex-shrink:0';
    menuBtn.onmouseenter = () => menuBtn.style.color = 'var(--text)';
    menuBtn.onmouseleave = () => menuBtn.style.color = 'var(--muted)';
    menuBtn.onclick = (e) => {
      e.stopPropagation();
      // Find or create a fake tab object for _openTabMenu
      let t = state.tabs.find(tab => tab.id === p.id);
      if (!t) { openTab(p); t = state.tabs.find(tab => tab.id === p.id); }
      if (t) _openTabMenu(t, menuBtn);
    };
    div.appendChild(nameSpan);
    div.appendChild(menuBtn);
    div.onclick = (e) => { if (e.target !== menuBtn) openTab(p); };
    list.appendChild(div);
  });
  // v1.9.x — hide the legacy dropdown; keep element for switcher.value compat.
  const switcher = document.getElementById('project-switcher');
  if (switcher) {
    switcher.style.display = 'none';
    const previous = switcher.value;
    switcher.innerHTML = '';
    state.projects.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id; opt.textContent = p.name;
      switcher.appendChild(opt);
    });
    if (previous && state.projects.some(p => p.id === previous)) switcher.value = previous;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// v0.5.1 — sessions render with relative timestamps so the user can
// see at a glance which workers are actually alive. SQLite stores
// timestamps in UTC without timezone markers; we treat them as UTC.
function formatRelativeTime(ts) {
  if (!ts) return '';
  const iso = ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z';
  const then = new Date(iso);
  const seconds = Math.max(0, Math.floor((Date.now() - then.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function openTab(project) {
  const existing = state.tabs.find(t => t.id === project.id);
  if (existing) { activateTab(project.id); return; }
  state.tabs.push({ id: project.id, project });
  saveTabs();
  renderTabs();
  buildTabBody(project);
  activateTab(project.id);
}

function closeTab(id) {
  state.tabs = state.tabs.filter(t => t.id !== id);
  const panel = state.panels[id];
  if (panel) {
    try { panel.ws && panel.ws.close(); } catch(e){}
    delete state.panels[id];
  }
  document.getElementById(`tab-body-${id}`)?.remove();
  saveTabs();
  renderTabs();
  if (state.activeTab === id) {
    const next = state.tabs[state.tabs.length - 1];
    state.activeTab = next ? next.id : null;
    if (next) activateTab(next.id);
    else document.getElementById('tab-bodies').innerHTML = '<div class="empty">no project open — pick one on the left</div>';
  }
}

function saveTabs() {
  try {
    localStorage.setItem(TABS_KEY, JSON.stringify(state.tabs.map(t => t.id)));
  } catch(e) {}
}

const TAB_OVERFLOW_THRESHOLD = 10;

function renderTabs() {
  const bar = document.getElementById('tabs');
  bar.innerHTML = '';

  // v1.9.x — overflow: show first (N-1) tabs + ">>" button if 10+
  const overflow = state.tabs.length >= TAB_OVERFLOW_THRESHOLD;
  const visible = overflow ? state.tabs.slice(0, TAB_OVERFLOW_THRESHOLD - 1) : state.tabs;
  const hidden  = overflow ? state.tabs.slice(TAB_OVERFLOW_THRESHOLD - 1) : [];

  visible.forEach(t => bar.appendChild(_makeTabEl(t)));

  if (overflow) {
    const more = document.createElement('div');
    more.className = 'tab tab-overflow';
    more.textContent = `>> ${hidden.length} more`;
    more.title = hidden.map(t => t.project.name).join(', ');
    // Click opens a small dropdown of hidden tabs.
    more.onclick = (e) => {
      e.stopPropagation();
      let menu = document.getElementById('tab-overflow-menu');
      if (menu) { menu.remove(); return; }
      menu = document.createElement('div');
      menu.id = 'tab-overflow-menu';
      menu.style.cssText = 'position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1000;min-width:160px;box-shadow:0 4px 12px rgba(0,0,0,0.4)';
      const rect = more.getBoundingClientRect();
      menu.style.top = (rect.bottom + 4) + 'px';
      menu.style.left = rect.left + 'px';
      hidden.forEach(t => {
        const item = document.createElement('div');
        item.style.cssText = 'padding:8px 12px;cursor:pointer;font-size:11px;font-family:var(--font-mono)';
        item.textContent = t.project.name;
        item.onmouseenter = () => item.style.background = 'var(--surface-1)';
        item.onmouseleave = () => item.style.background = '';
        item.onclick = () => { menu.remove(); activateTab(t.id); };
        menu.appendChild(item);
      });
      document.body.appendChild(menu);
      const close = () => { menu.remove(); document.removeEventListener('click', close); };
      setTimeout(() => document.addEventListener('click', close), 0);
    };
    bar.appendChild(more);
  }
}

function _makeTabEl(t) {
  const div = document.createElement('div');
  div.className = 'tab' + (state.activeTab === t.id ? ' active' : '');
  div.dataset.tabId = t.id;
  div.onclick = () => activateTab(t.id);

  const nameSpan = document.createElement('span');
  nameSpan.textContent = t.project.name;
  div.appendChild(nameSpan);

  // v1.9.x — kebab ⋯ menu
  const kebab = document.createElement('button');
  kebab.className = 'tab-kebab';
  kebab.textContent = '⋯';
  kebab.title = 'Project actions';
  kebab.onclick = (e) => { e.stopPropagation(); _openTabMenu(t, kebab); };
  div.appendChild(kebab);

  const close = document.createElement('button');
  close.className = 'close';
  close.textContent = '×';
  close.onclick = (e) => { e.stopPropagation(); closeTab(t.id); };
  div.appendChild(close);

  return div;
}

function _openTabMenu(t, anchor) {
  // Close any existing menu.
  document.querySelectorAll('.tab-context-menu').forEach(m => m.remove());

  const menu = document.createElement('div');
  menu.className = 'tab-context-menu';
  menu.style.cssText = 'position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1001;min-width:150px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:11px;font-family:var(--font-mono)';

  function menuItem(label, fn) {
    const item = document.createElement('div');
    item.style.cssText = 'padding:8px 12px;cursor:pointer';
    item.textContent = label;
    item.onmouseenter = () => { item.style.background = 'var(--surface-3)'; item.style.color = 'var(--accent)'; };
    item.onmouseleave = () => { item.style.background = ''; item.style.color = ''; };
    item.onclick = () => { menu.remove(); fn(); };
    menu.appendChild(item);
  }

  // UUID header
  const uuidDiv = document.createElement('div');
  uuidDiv.style.cssText = 'padding:6px 12px;color:var(--muted);font-size:10px;border-bottom:1px solid var(--border);user-select:all;cursor:text';
  uuidDiv.textContent = t.id;
  menu.appendChild(uuidDiv);
  menuItem('\u270f Rename', () => _renameProject(t));
  menuItem('\u2b07 Download DB', () => window.open('/admin/snapshot', '_blank'));
  menuItem('🗑 Delete project…', () => _deleteProject(t));

  const rect = anchor.getBoundingClientRect();
  menu.style.top = (rect.bottom + 4) + 'px';
  menu.style.left = rect.left + 'px';
  document.body.appendChild(menu);
  const dismiss = () => { menu.remove(); document.removeEventListener('click', dismiss); };
  setTimeout(() => document.addEventListener('click', dismiss), 0);
}

async function _renameProject(t) {
  const newName = window.prompt(`Rename "${t.project.name}" to:`, t.project.name);
  if (!newName || newName.trim() === t.project.name) return;
  try {
    const updated = await api(`/projects/${t.id}/rename`, {
      method: 'POST', body: JSON.stringify({ name: newName.trim() }),
    });
    t.project = { ...t.project, name: updated.name };
    // Update body header if open.
    const hdr = document.querySelector(`#drawer-status-${t.id} .drawer-header span:first-child`);
    if (hdr) hdr.textContent = 'STATUS · ' + updated.name;
    renderTabs();
    toast('Renamed to ' + updated.name);
  } catch(e) { toast('Rename failed: ' + e.message, true); }
}

async function _deleteProject(t) {
  const confirmed = window.confirm(
    `Delete "${t.project.name}"?\n\nThis will permanently delete all sessions, tasks, and goal history. Cannot be undone.`
  );
  if (!confirmed) return;
  try {
    await api(`/projects/${t.id}`, { method: 'DELETE' });
    closeTab(t.id);
    state.projects = state.projects.filter(p => p.id !== t.id);
    await loadProjects();
    toast('Project deleted');
  } catch(e) {
    // 409 = in_progress tasks
    toast(e.message.includes('409') ? e.message : 'Delete failed: ' + e.message, true);
  }
}

function activateTab(id) {
  state.activeTab = id;
  renderTabs();
  document.querySelectorAll('.tab-body').forEach(el => el.classList.remove('active'));
  const body = document.getElementById(`tab-body-${id}`);
  if (body) body.classList.add('active');
  // clear empty placeholder
  const empty = document.querySelector('.tab-bodies > .empty');
  if (empty) empty.remove();
  // Persist active project so a refresh reopens to the same tab.
  try { localStorage.setItem(ACTIVE_PROJECT_KEY, id); } catch(e) {}
  // Keep the sidebar dropdown in sync with whichever tab the user is on.
  const switcher = document.getElementById('project-switcher');
  if (switcher) switcher.value = id;
}

function buildTabBody(project) {
  const root = document.getElementById('tab-bodies');
  const empty = root.querySelector(':scope > .empty');
  if (empty) empty.remove();

  const body = document.createElement('div');
  body.className = 'tab-body';
  body.id = `tab-body-${project.id}`;
  body.innerHTML = `
    <div class="vtab-strip" id="vtab-strip-${project.id}">
      <button class="vtab-btn active" data-vtab="status" title="Status &amp; Sessions">≡</button>
      <button class="vtab-btn" data-vtab="live" title="Live — right-now view">⚡</button>
      <button class="vtab-btn" data-vtab="goal" title="Goal State">◎</button>
      <button class="vtab-btn" data-vtab="files" title="Files">⊞</button>
      <button class="vtab-btn" data-vtab="devlog" title="Dev Log">≋</button>
      <button class="vtab-btn" data-vtab="timeline" title="Activity Timeline">⌬</button>
      <button class="vtab-btn" data-vtab="rewind" title="Rewind — Last X days">↻</button>
      <button class="vtab-btn" data-vtab="queue" title="Work Queue">⚙</button>
    </div>
    <div class="vtab-drawer open" id="drawer-${project.id}">
      <div class="drawer-panel active" id="drawer-status-${project.id}">
        <div class="drawer-header">
          <span>STATUS · ${escapeHtml(project.name)}</span>
          <span class="ws-dot" id="ws-${project.id}"></span>
        </div>
        <div class="section">
          <h3>Active Sessions</h3>
          <div class="sessions-list" id="sessions-${project.id}"></div>
        </div>
        <div class="hitl-banner" id="hitl-banner-${project.id}" style="display:none">HITL queue</div>
        <div id="hitl-queue-${project.id}"></div>
      </div>
      <div class="drawer-panel" id="drawer-live-${project.id}">
        <div class="drawer-header" style="justify-content:space-between">
          <span>LIVE · ${escapeHtml(project.name)}</span>
          <span style="display:flex;gap:6px;align-items:center">
            <button class="secondary" id="live-auto-btn-${project.id}" title="Toggle auto-refresh" style="padding:2px 8px;font-size:10px">↻ Auto</button>
            <button class="secondary" id="live-pause-${project.id}" title="Pause queue (UI stub)" style="padding:2px 8px;font-size:10px">Pause</button>
            <button class="secondary" id="live-run-${project.id}" title="Run all pending (UI stub)" style="padding:2px 8px;font-size:10px">Run All</button>
          </span>
        </div>
        <div class="live-body" id="live-body-${project.id}">
          <div class="live-section">
            <div class="live-section-label">Sprint progress</div>
            <div id="live-sprint-progress-${project.id}" class="live-sprint-progress"></div>
          </div>
          <hr class="live-divider">
          <div class="live-section">
            <div class="live-section-label">Active sessions</div>
            <div class="live-sessions" id="live-sessions-${project.id}">
              <div class="live-empty">No active sessions.</div>
            </div>
          </div>
          <hr class="live-divider">
          <div class="live-section">
            <div class="live-section-label">Queue</div>
            <div class="live-queue" id="live-queue-${project.id}">
              <div class="live-empty">Queue is empty. Add a task above.</div>
            </div>
            <div class="live-add-row">
              <input type="text" class="live-add-input" id="live-add-input-${project.id}" placeholder="+ Add task… (Enter to submit)">
            </div>
          </div>
        </div>
      </div>
      <div class="drawer-panel" id="drawer-goal-${project.id}">
        <div class="drawer-header" style="justify-content:space-between">
          <span>GOAL · ${escapeHtml(project.name)}</span>
          <span style="display:flex;gap:6px;align-items:center">
            <span class="goal-version" id="goal-version-${project.id}"></span>
          </span>
        </div>
        <div class="goal-subtab-strip">
          <button class="goal-subtab-btn active" data-gtab="north-star">🔭 North Star</button>
          <button class="goal-subtab-btn" data-gtab="version-goal">◎ Version Goal</button>
          <button class="goal-subtab-btn" data-gtab="sprint">⚡ Sprint</button>
          <button class="goal-subtab-btn" data-gtab="decisions">📋 Decisions</button>
        </div>
        <div class="goal-subtab-body">
          <div class="goal-subtab-panel active" id="gtab-north-star-${project.id}">
            <textarea class="goal-area goal-full mono" id="goal-north-star-${project.id}" placeholder="(north star not set — set once, rarely change)"></textarea>
            <div class="goal-actions">
              <button class="primary" id="save-north-star-${project.id}">save north star</button>
              <span class="goal-ts" id="goal-ns-ts-${project.id}"></span>
              <span id="goal-ns-lock-${project.id}" style="opacity:0.5;font-size:11px"></span>
            </div>
          </div>
          <div class="goal-subtab-panel" id="gtab-version-goal-${project.id}">
            <div id="goal-title-${project.id}" style="font-family:var(--font-mono);font-size:11px;font-weight:600;color:var(--accent);padding:5px 8px;background:var(--surface-2);border:1px solid var(--border);border-radius:4px 4px 0 0;border-bottom:none;user-select:none;flex-shrink:0" title="Version title (read-only)"></div>
            <div id="goal-shipped-${project.id}" style="display:none;font-family:var(--font-mono);font-size:10px;color:var(--muted);padding:6px 8px;background:var(--surface-2);border:1px solid var(--border);border-top:none;border-bottom:none;white-space:pre-wrap;user-select:none;flex-shrink:0" title="SHIPPED section (read-only — updated by Claude Code)"></div>
            <textarea class="goal-area goal-full mono" id="goal-${project.id}" placeholder="CURRENT FOCUS:" style="border-radius:0 0 4px 4px;font-size:13px"></textarea>
            <div class="goal-actions" style="flex-shrink:0">
              <button class="primary" id="save-goal-${project.id}">save version goal</button>
              <span class="goal-version" id="goal-state-${project.id}"></span>
              <span class="goal-ts" id="goal-vg-ts-${project.id}"></span>
            </div>
            <div id="goal-autoblocks-wrapper-${project.id}" style="display:none;flex-shrink:0">
              <button onclick="(function(b,c){var open=c.style.display!=='none';c.style.display=open?'none':'block';b.textContent=open?'📋 Session Log ▶':'📋 Session Log ▼';})(this,document.getElementById('goal-autoblocks-${project.id}'))" style="background:none;border:none;color:var(--muted);font-size:10px;font-weight:600;cursor:pointer;padding:2px 0;font-family:var(--font-mono);margin-top:6px">📋 Session Log ▶</button>
              <div id="goal-autoblocks-${project.id}" style="display:none;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:8px;font-family:var(--font-mono);font-size:13px;color:var(--text);white-space:pre-wrap;word-break:break-word;margin-top:4px"></div>
            </div>
          </div>
          <div class="goal-subtab-panel" id="gtab-sprint-${project.id}">
            <div style="color:var(--muted);font-size:10px;margin-bottom:4px">Sprint header:</div>
            <div style="display:flex;gap:6px;margin-bottom:10px;align-items:center">
              <input type="text" id="goal-sprint-${project.id}" placeholder="v1.9.x — description" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">
              <button class="secondary" id="save-sprint-${project.id}" style="white-space:nowrap">save</button>
              <span class="goal-ts" id="goal-sp-ts-${project.id}" style="font-size:10px;color:var(--muted)"></span>
            </div>
            <div style="color:var(--muted);font-size:10px;margin-bottom:6px">Sprint tasks:</div>
            <div id="sprint-board-goal-${project.id}"></div>
            <div style="display:flex;gap:6px;margin-top:8px">
              <input type="text" id="sprint-add-input-${project.id}" placeholder="Add task to sprint..." style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">
              <button class="secondary" id="sprint-add-btn-${project.id}">+ Add</button>
            </div>
          </div>
          <div class="goal-subtab-panel" id="gtab-decisions-${project.id}">
            <div style="color:var(--muted);font-size:10px;margin-bottom:8px">Append-only decisions log (newest first). Read-only — log decisions via the <code>set_decision</code> MCP tool.</div>
            <div id="decisions-table-${project.id}" style="font-family:var(--font-mono);font-size:12px"></div>
          </div>
        </div>
        <div style="flex-shrink:0;padding:8px 14px;border-top:1px solid var(--border)">
          <a class="secondary" style="display:inline-block;padding:5px 12px;border:1px solid var(--border);border-radius:4px;color:var(--muted);font-size:10px;text-decoration:none;font-family:'IBM Plex Mono',monospace;cursor:pointer" href="/projects/${project.id}/export/pdf" download>⬇ Export IP Record (PDF)</a>
        </div>
      </div>
      <div class="drawer-panel" id="drawer-files-${project.id}">
        <div class="drawer-header">FILES · ${escapeHtml(project.name)}</div>
        <div id="files-browse-${project.id}" style="flex:1;overflow-y:auto">
          <div class="file-list" id="files-list-${project.id}"></div>
        </div>
        <div id="file-editor-wrap-${project.id}" style="display:none;flex:1;flex-direction:column;overflow:hidden">
          <div class="drawer-header" style="flex-shrink:0">
            <button class="secondary" id="file-back-${project.id}" style="padding:2px 8px;font-size:10px">← back</button>
            <span id="file-name-${project.id}" style="flex:1;color:var(--accent);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
            <button class="primary" id="file-save-${project.id}" style="padding:2px 8px;font-size:10px">save</button>
          </div>
          <div class="preview-toggle-row" style="padding:6px 14px 4px;flex-shrink:0" id="file-toggle-row-${project.id}">
            <button class="preview-btn active" data-fmode="edit" id="file-mode-edit-${project.id}">edit</button>
            <button class="preview-btn" data-fmode="preview" id="file-mode-preview-${project.id}">preview</button>
          </div>
          <textarea id="file-content-${project.id}" style="flex:1;background:var(--surface-2);border:none;border-top:1px solid var(--border);color:var(--text);padding:10px 14px;font-family:'IBM Plex Mono',monospace;font-size:12px;resize:none;outline:none;overflow-y:auto"></textarea>
          <div id="file-preview-${project.id}" class="goal-preview" style="display:none;flex:1;margin:10px 14px;"></div>
        </div>
      </div>
      <div class="drawer-panel" id="drawer-devlog-${project.id}">
        <div class="drawer-header">DEV LOG · ${escapeHtml(project.name)}</div>
        <div class="scroll-area"><div class="task-list" id="tasks-${project.id}"></div></div>
      </div>
      <div class="drawer-panel" id="drawer-timeline-${project.id}">
        <div class="drawer-header" style="justify-content:space-between">
          <span>TIMELINE · ${escapeHtml(project.name)}</span>
          <span style="display:flex;gap:6px;align-items:center">
            <button class="secondary" id="timeline-axis-${project.id}" title="Toggle relative/absolute time" style="padding:2px 8px;font-size:10px">relative</button>
            <button class="secondary" id="timeline-refresh-${project.id}" title="Refresh" style="padding:2px 8px;font-size:10px">refresh</button>
          </span>
        </div>
        <div class="timeline-wrap" id="timeline-wrap-${project.id}" style="flex:1;overflow:auto;padding:14px"></div>
      </div>
      <div class="drawer-panel" id="drawer-rewind-${project.id}">
        <div class="drawer-header" style="justify-content:space-between">
          <span>REWIND · ${escapeHtml(project.name)}</span>
          <span style="display:flex;gap:6px;align-items:center">
            <button class="secondary rewind-day-btn" data-days="7" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">7d</button>
            <button class="secondary rewind-day-btn" data-days="14" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">14d</button>
            <button class="secondary rewind-day-btn" data-days="30" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">30d</button>
            <button class="secondary rewind-day-btn" data-days="90" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">90d</button>
          </span>
        </div>
        <div class="rewind-wrap" id="rewind-wrap-${project.id}" style="flex:1;overflow:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:11px">
          <div class="empty" style="color:var(--muted)">pick a window above</div>
        </div>
        <div style="flex-shrink:0;padding:8px 14px;border-top:1px solid var(--border);display:flex;gap:8px">
          <button class="secondary" id="rewind-share-${project.id}" style="padding:4px 10px;font-size:10px">Copy shareable link</button>
          <a class="secondary" href="/projects/${project.id}/export/pdf" download
             style="padding:4px 10px;font-size:10px;border:1px solid var(--border);border-radius:4px;color:var(--muted);text-decoration:none;font-family:'IBM Plex Mono',monospace">Export as PDF</a>
        </div>
      </div>
      <div class="drawer-panel" id="drawer-queue-${project.id}">
        <div class="drawer-header" style="justify-content:space-between">
          <span>QUEUE · ${escapeHtml(project.name)}</span>
          <button class="secondary" id="queue-refresh-${project.id}" style="padding:2px 8px;font-size:10px">refresh</button>
        </div>
        <div style="flex:1;overflow-y:auto" id="queue-body-${project.id}">
          <div class="empty" style="color:var(--muted)">select queue to load</div>
        </div>
      </div>
    </div>
    <section class="claude-handoff-panel">
      <div class="panel-header">
        <span>CLAUDE</span>
        <span class="server-version-pill" id="server-version"></span>
      </div>
      <div class="claude-launch-body">
        <div class="claude-section" data-section="continue">
          <div class="claude-section-label">Resume Claude Code session</div>
          <select class="claude-session-select" id="continue-session-${project.id}">
            <option value="">(no sessions yet)</option>
          </select>
          <button class="primary claude-section-btn" id="copy-resume-${project.id}">Copy resume command</button>
          <p class="claude-hint">Paste into Claude Code to continue a previous session</p>
        </div>
        <hr class="claude-divider">
        <div class="claude-section" data-section="worker">
          <div class="claude-section-label">Start Claude Code worker</div>
          <button class="primary claude-section-btn" id="start-worker-${project.id}">Claim &amp; start worker</button>
          <div class="claude-worker-result" id="worker-result-${project.id}" style="display:none">
            <pre class="claude-worker-xml" id="worker-xml-${project.id}"></pre>
            <button class="secondary claude-section-btn" id="copy-worker-${project.id}">Copy worker context</button>
            <p class="claude-hint">Paste into a new Claude Code terminal to start a worker</p>
          </div>
          <div class="claude-worker-empty" id="worker-empty-${project.id}" style="display:none">
            <p class="claude-hint">No pending tasks — add one to the queue first.</p>
          </div>
        </div>
        <hr class="claude-divider">
        <div class="claude-section" data-section="handoff">
          <div class="claude-section-label">Claude Code handoff</div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <button class="primary claude-section-btn" id="copy-handoff-${project.id}">Code Handoff</button>
            <button class="secondary claude-section-btn" id="regen-handoff-${project.id}">Regenerate</button>
            <span class="claude-handoff-ts" id="handoff-ts-${project.id}" style="font-size:10px;color:var(--muted)"></span>
          </div>
          <p class="claude-hint">Copy and paste into Claude Code to resume with full context</p>
        </div>
        <hr class="claude-divider">
        <div class="claude-section claude-section-narrow" data-section="open">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <a class="claude-cta-secondary-btn" id="open-in-claude-${project.id}"
               href="https://claude.ai/new" target="_blank" rel="noopener">New Chat →</a>
            <button class="secondary claude-section-btn" id="copy-context-${project.id}" style="font-size:11px">Copy chat context</button>
          </div>
          <p class="claude-hint">Open a new Claude.ai chat, paste the context to get up to speed</p>
        </div>
      </div>
      </div>
    </section>
  `;
  root.appendChild(body);

  // Per-tab state. chatMode restored from localStorage so the user's choice
  // persists across reloads. activeVtab tracks which drawer panel is open.
  let initialMode = 'api';
  try {
    const saved = localStorage.getItem('meridian.chatMode');
    if (saved === 'api' || saved === 'cli') initialMode = saved;
  } catch(e) {}
  state.panels[project.id] = {
    ws: null, taskCache: [], goalRaw: null, goalIsJson: false,
    chatHistory: [], chatMode: initialMode, activeVtab: 'status',
  };

  // Vtab drawer toggle — same tab again collapses; different tab switches.
  const vtabStrip = document.getElementById(`vtab-strip-${project.id}`);
  const drawer = document.getElementById(`drawer-${project.id}`);
  if (vtabStrip && drawer) {
    vtabStrip.querySelectorAll('.vtab-btn').forEach(btn => {
      btn.onclick = () => {
        const vtab = btn.dataset.vtab;
        const p = state.panels[project.id];
        // v1.4.0: drawer is always visible — just switch active panel.
        vtabStrip.querySelectorAll('.vtab-btn').forEach(b => {
          b.classList.toggle('active', b.dataset.vtab === vtab);
        });
        drawer.querySelectorAll('.drawer-panel').forEach(dp => {
          dp.classList.toggle('active', dp.id === `drawer-${vtab}-${project.id}`);
        });
        p.activeVtab = vtab;
        if (vtab === 'files') loadFilesTab(project.id);
        if (vtab === 'devlog') refreshTasks(project.id);
        if (vtab === 'timeline') loadTimeline(project.id);
        if (vtab === 'rewind') initRewindTab(project.id);
        if (vtab === 'queue') loadQueue(project.id);
        if (vtab === 'live') loadLiveTab(project.id);
      };
    });
  }

  // Goal subtab switching (North Star / Version Goal / Sprint)
  const goalDrawer = document.getElementById(`drawer-goal-${project.id}`);
  if (goalDrawer) {
    goalDrawer.querySelectorAll('.goal-subtab-btn').forEach(btn => {
      btn.onclick = () => {
        goalDrawer.querySelectorAll('.goal-subtab-btn').forEach(b => b.classList.toggle('active', b === btn));
        const gtab = btn.dataset.gtab;
        goalDrawer.querySelectorAll('.goal-subtab-panel').forEach(p => {
          p.classList.toggle('active', p.id === `gtab-${gtab}-${project.id}`);
        });
      };
    });
  }

  document.getElementById(`save-goal-${project.id}`).onclick = () => saveGoal(project.id);
  document.getElementById(`save-north-star-${project.id}`).onclick = () => saveNorthStar(project.id);
  document.getElementById(`save-sprint-${project.id}`).onclick = () => saveSprint(project.id);

  // Sprint tab board — load and render sprint items
  async function loadSprintBoard() {
    try {
      const items = await api(`/projects/${project.id}/sprint-items`);
      const board = document.getElementById(`sprint-board-goal-${project.id}`);
      if (!board) return;
      if (!items || !items.length) {
        board.innerHTML = '<div style="color:var(--muted);font-size:10px;padding:4px 0">(no sprint items — add one below)</div>';
        return;
      }
      // Group by version — current sprint expanded, older collapsed
      const currentSprint = (state.panels[project.id] || {}).sprint || '';
      const byVersion = {};
      items.forEach(it => {
        const v = it.version || 'unversioned';
        if (!byVersion[v]) byVersion[v] = [];
        byVersion[v].push(it);
      });
      const versions = Object.keys(byVersion).sort((a, b) => b.localeCompare(a));
      board.innerHTML = versions.map((v, vi) => {
        const vItems = byVersion[v];
        const isCurrentV = vi === 0 || currentSprint.includes(v);
        const doneCount = vItems.filter(it => it.status === 'done' || it.status === 'skipped').length;
        const rows = vItems.map(it => {
          const done = it.status === 'done' || it.status === 'skipped';
          const color = done ? 'var(--status-done)' : it.status === 'failed' ? 'var(--status-failed)' : 'var(--text)';
          const strike = done ? 'text-decoration:line-through;opacity:0.4;' : '';
          return `<div style="display:flex;align-items:flex-start;gap:6px;padding:3px 0;border-bottom:1px solid var(--border)">
            <span style="font-size:10px;${strike}color:${color};flex:1;word-break:break-word">${escapeHtml(it.title)}</span>
            ${!done ? `<button onclick="_sprintAction('${project.id}','${it.id}','complete')" title="Done" style="background:none;border:1px solid var(--status-done);color:var(--status-done);border-radius:3px;cursor:pointer;font-size:10px;padding:1px 5px">✓</button>
            <button onclick="_deleteSprintItem('${project.id}','${it.id}')" title="Delete" style="background:none;border:1px solid var(--status-failed);color:var(--status-failed);border-radius:3px;cursor:pointer;font-size:10px;padding:1px 5px">✗</button>` : ''}
          </div>`;
        }).join('');
        return `<details ${isCurrentV ? 'open' : ''} style="margin-bottom:4px">
          <summary style="font-size:10px;font-weight:600;color:${isCurrentV ? 'var(--accent)' : 'var(--muted)'};cursor:pointer;padding:3px 0;list-style:none;display:flex;justify-content:space-between;user-select:none">
            <span>${escapeHtml(v)}</span>
            <span style="font-weight:400;opacity:0.6">${doneCount}/${vItems.length}</span>
          </summary>
          <div style="padding-left:4px">${rows}</div>
        </details>`;
      }).join('');
    } catch(e) { console.error('Sprint board load failed:', e); }
  }
  _sprintBoardReloaders[project.id] = loadSprintBoard;
  loadSprintBoard();

  const sprintAddBtn = document.getElementById(`sprint-add-btn-${project.id}`);
  const sprintAddInput = document.getElementById(`sprint-add-input-${project.id}`);
  if (sprintAddBtn && sprintAddInput) {
    const doAdd = async () => {
      const title = sprintAddInput.value.trim();
      if (!title) return;
      try {
        await api(`/projects/${project.id}/sprint-items`, { method: 'POST', body: JSON.stringify({ title, version: state.panels[project.id]?.sprint || 'current' }) });
        sprintAddInput.value = '';
        loadSprintBoard();
      } catch(e) { console.error('Add sprint item failed:', e); }
    };
    sprintAddBtn.onclick = doAdd;
    sprintAddInput.onkeydown = (e) => { if (e.key === 'Enter') doAdd(); };
  }

  // v1.6.x — goal-mode toggle removed. Auto mode (v0.4.2) appended ambient
  // task summaries to goal content; that role is now covered by the timeline
  // + session summaries + task log. Manual is the only mode worth exposing.

  // v1.1.0 — chat panel removed in favour of "Open in Claude". The
  // wireups below guard for null elements so older deployments with
  // the chat surface still mounted keep working.
  const clearChatBtn = document.getElementById(`clear-chat-${project.id}`);
  if (clearChatBtn) clearChatBtn.onclick = async () => {
    if (!confirm('Clear chat history for this project?')) return;
    try {
      await fetch(`/projects/${project.id}/chat/history`, { method: 'DELETE' });
      const history = document.getElementById(`chat-${project.id}`);
      if (history) history.innerHTML = '';
      if (state.panels[project.id]) state.panels[project.id].chatHistory = [];
      toast('chat cleared');
    } catch(e) { toast('clear failed: ' + e.message, true); }
  };
  const modelSel = document.getElementById(`model-select-${project.id}`);
  if (modelSel) {
    const VALID_MODELS = ['claude-sonnet-4-6','claude-opus-4-6','claude-opus-4-7','claude-haiku-4-5-20251001'];
    let savedModel = localStorage.getItem('meridian.chatModel') || 'claude-sonnet-4-6';
    if (!VALID_MODELS.includes(savedModel)) savedModel = 'claude-sonnet-4-6';
    modelSel.value = savedModel;
    if (state.panels[project.id]) state.panels[project.id].chatModel = savedModel;
    modelSel.onchange = () => {
      const m = modelSel.value;
      if (state.panels[project.id]) state.panels[project.id].chatModel = m;
      localStorage.setItem('meridian.chatModel', m);
      toast('model: ' + m);
    };
  }
  // v1.5.x — Claude launch control panel (4 sections).
  wireClaudeLaunchPanel(project.id);
  document.getElementById(`goal-${project.id}`).addEventListener('blur', () => saveGoal(project.id));
  document.getElementById(`goal-north-star-${project.id}`).addEventListener('blur', () => saveNorthStar(project.id));
  document.getElementById(`goal-sprint-${project.id}`).addEventListener('blur', () => saveSprint(project.id));
  // v0.6.4 — dirty state: highlight textarea border when unsaved changes exist
  document.getElementById(`goal-${project.id}`).addEventListener('input', function() {
    const p = state.panels[project.id];
    this.classList.toggle('dirty', this.value !== (p._lastSaved || ''));
  });
  document.getElementById(`goal-north-star-${project.id}`).addEventListener('input', function() {
    const p = state.panels[project.id];
    this.classList.toggle('dirty', this.value !== (p._serverNorthStar || ''));
  });
  document.getElementById(`goal-sprint-${project.id}`).addEventListener('input', function() {
    const p = state.panels[project.id];
    this.classList.toggle('dirty', this.value !== (p._serverSprint || ''));
  });
  // v1.1.0 — chat input + mode toggle removed. Defensive wireups for
  // anyone running an older bundle: only attach if the elements exist.
  const chatSendBtn = document.getElementById(`chat-send-${project.id}`);
  if (chatSendBtn) chatSendBtn.onclick = () => sendChat(project.id);
  const input = document.getElementById(`chat-input-${project.id}`);
  if (input) input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(project.id); }
  });
  const modeRoot = document.getElementById(`chat-mode-${project.id}`);
  if (modeRoot) {
    modeRoot.querySelectorAll('.mode-btn').forEach(btn => {
      if (btn.dataset.mode === initialMode) btn.classList.add('active');
      else btn.classList.remove('active');
      btn.onclick = () => {
        const mode = btn.dataset.mode;
        state.panels[project.id].chatMode = mode;
        try { localStorage.setItem('meridian.chatMode', mode); } catch(e) {}
        modeRoot.querySelectorAll('.mode-btn').forEach(b => {
          b.classList.toggle('active', b.dataset.mode === mode);
        });
        toast(mode === 'cli'
          ? 'CLI mode — uses Max plan, no API credits'
          : 'API mode — bills metered API credits');
      };
    });
  }

  // Files tab: back button returns to browse view; save button persists edits.
  const fileBackBtn = document.getElementById(`file-back-${project.id}`);
  const fileSaveBtn = document.getElementById(`file-save-${project.id}`);
  if (fileBackBtn) fileBackBtn.onclick = () => {
    const browse = document.getElementById(`files-browse-${project.id}`);
    const editorWrap = document.getElementById(`file-editor-wrap-${project.id}`);
    if (browse) browse.style.display = '';
    if (editorWrap) editorWrap.style.display = 'none';
  };
  if (fileSaveBtn) fileSaveBtn.onclick = () => saveFile(project.id);

  refreshTab(project.id);

  // Restore persisted chat history so conversations survive page refresh.
  (async () => {
    try {
      const history = await api(`/projects/${project.id}/chat/history`);
      if (history && history.length) {
        const chatDiv = document.getElementById(`chat-${project.id}`);
        const panel = state.panels[project.id];
        if (chatDiv && panel) {
          history.forEach(msg => {
            appendChatMessage(chatDiv, msg.role, msg.content);
            panel.chatHistory.push({ role: msg.role, content: msg.content });
          });
        }
      }
    } catch(e) { /* no history yet or endpoint not reachable */ }
  })();

  connectWs(project.id);
}

// v1.6.x — LIVE tab. Right-now view of active sessions + work queue.
// Section A: active sessions (filtered to last 24h) with claimed task
// per session, freshness dot, and human_id badge.
// Section B: pending + in_progress tasks, [+ Add task] input, cancel
// per row. WS-driven + optional 30s auto-refresh (v1.7.0).

const LIVE_REFRESH_MS = 30000;
const LIVE_THROTTLE_MS = 10000;
const liveRefreshState = {}; // keyed by projectId

function scheduleLiveRefresh(projectId) {
  const s = liveRefreshState[projectId] || (liveRefreshState[projectId] = {});
  clearTimeout(s.timer);
  if (!s.enabled) return;
  const sinceLastMs = Date.now() - (s.lastRefresh || 0);
  const wait = Math.max(LIVE_THROTTLE_MS, LIVE_REFRESH_MS - sinceLastMs);
  s.timer = setTimeout(async () => {
    s.lastRefresh = Date.now();
    const panel = state.panels[projectId];
    if (panel && panel.activeVtab === 'live') {
      await refreshLiveTab(projectId);
    }
    scheduleLiveRefresh(projectId);
  }, wait);
}

function initLiveAutoRefresh(projectId) {
  const s = liveRefreshState[projectId] || (liveRefreshState[projectId] = {});
  const stored = localStorage.getItem('meridian.liveAutoRefresh');
  s.enabled = stored === null ? true : stored === 'true';
  const btn = document.getElementById(`live-auto-btn-${projectId}`);
  if (btn) {
    btn.textContent = s.enabled ? '↻ Auto' : '↻ Off';
    btn.style.opacity = s.enabled ? '1' : '0.4';
    btn.onclick = () => {
      s.enabled = !s.enabled;
      localStorage.setItem('meridian.liveAutoRefresh', String(s.enabled));
      btn.textContent = s.enabled ? '↻ Auto' : '↻ Off';
      btn.style.opacity = s.enabled ? '1' : '0.4';
      if (s.enabled) scheduleLiveRefresh(projectId);
      else clearTimeout(s.timer);
    };
  }
  if (s.enabled) scheduleLiveRefresh(projectId);
}

async function loadLiveTab(projectId) {
  const panel = state.panels[projectId];
  if (!panel) return;
  panel.liveWired = panel.liveWired || false;
  // Wire the [Pause]/[Run All] header stubs once.
  if (!panel.liveWired) {
    const pause = document.getElementById(`live-pause-${projectId}`);
    const runAll = document.getElementById(`live-run-${projectId}`);
    if (pause) pause.onclick = () => toast('Pause is a stub — coming soon');
    if (runAll) runAll.onclick = () => toast('Run All is a stub — coming soon');
    const input = document.getElementById(`live-add-input-${projectId}`);
    if (input) input.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Enter') return;
      ev.preventDefault();
      const text = (input.value || '').trim();
      if (!text) return;
      addLiveTask(projectId, text).then((ok) => {
        if (ok) input.value = '';
      });
    });
    panel.liveWired = true;
  }
  await refreshLiveTab(projectId);
  initLiveAutoRefresh(projectId);
}

async function refreshLiveTab(projectId) {
  /** Fetch fresh sessions + tasks + sprint items and repaint all Live sections. */
  try {
    const [sessions, tasks, sprintItems] = await Promise.all([
      api(`/projects/${projectId}/sessions?active_only=false`).catch(() => []),
      api(`/projects/${projectId}/tasks?limit=200`).catch(() => []),
      api(`/projects/${projectId}/sprint-items`).catch(() => []),
    ]);
    renderSprintProgress(projectId, sprintItems || []);
    renderLiveSessions(projectId, sessions || [], tasks || []);
    renderLiveQueue(projectId, tasks || []);
    cacheMostRecentSession(projectId, sessions || []);
  } catch(e) { /* ignore — WS will retry on next event */ }
}

function renderSprintProgress(projectId, items) {
  /** Full grouped sprint board — replaces the old plain progress bar. */
  const root = document.getElementById(`live-sprint-progress-${projectId}`);
  if (!root) return;

  const statusIcon = s => ({
    pending: '○', todo: '○', in_progress: '◑',
    done: '●', failed: '✕', skipped: '—', pushed: '→'
  }[s] || '?');
  const statusColor = s => ({
    pending: 'var(--muted)', todo: 'var(--muted)',
    in_progress: 'var(--accent)',
    done: 'var(--accent-green)',
    failed: '#e05',
    skipped: 'var(--muted)',
    pushed: 'var(--accent)'
  }[s] || 'var(--muted)');
  const activeSet = new Set(['pending', 'todo', 'in_progress']);

  if (items.length === 0) {
    root.innerHTML = `
      <div class="live-empty">No sprint items. Add one below.</div>
      <div class="sprint-add-row" style="margin-top:6px">
        <input class="live-add-input" id="sprint-add-input-${projectId}"
               placeholder="version:title (e.g. v1.9:My item)">
        <button class="secondary sprint-add-btn" data-pid="${escapeHtml(projectId)}"
                style="margin-left:4px">+ Add</button>
      </div>`;
    root.querySelector('.sprint-add-btn').onclick =
      () => addSprintItemFromInput(projectId);
    wireSprintAddEnter(projectId, root);
    return;
  }

  // Show pending/in_progress always; show done only if recently completed (same version)
  // Get current version from sprint header
  const sprintHeader = document.querySelector(`#live-sprint-progress-${projectId}`)?.closest('.panel')
    ?.querySelector('.sprint-version')?.textContent || '';
  const activeStatuses = new Set(['pending', 'todo', 'in_progress']);
  const visibleItems = items.filter(it =>
    activeStatuses.has(it.status) ||
    (it.status === 'done' && it.version && items.some(x => activeStatuses.has(x.status) && x.version === it.version))
  );
  const displayItems = visibleItems.length > 0 ? visibleItems : items.filter(it => activeStatuses.has(it.status));

  // Group by item_group; ungrouped first.
  const groups = new Map();
  (displayItems.length > 0 ? displayItems : items).forEach(it => {
    const g = it.item_group || '';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(it);
  });

  let html = '';
  for (const [groupName, groupItems] of groups) {
    if (groupName) {
      html += `<div class="sprint-group-header">${escapeHtml(groupName)}</div>`;
    }
    html += groupItems.map(it => {
      const icon = statusIcon(it.status);
      const color = statusColor(it.status);
      const isActive = activeSet.has(it.status);
      const meta = it.pushed_to
        ? `<span class="sprint-item-meta">→ ${escapeHtml(it.pushed_to)}</span>`
        : (it.notes ? `<span class="sprint-item-meta">${escapeHtml(it.notes.slice(0,60))}</span>` : '');
      const actions = isActive
        ? `<span class="sprint-item-actions">
             <button class="sprint-btn" title="Done"
               onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">✓</button>
             <button class="sprint-btn" title="Skip"
               onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','skip')">—</button>
             <button class="sprint-btn sprint-btn-fail" title="Fail"
               onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','fail')">✕</button>
             <button class="sprint-btn sprint-btn-push" title="Push to next version"
               onclick="sprintPushPrompt('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">→</button>
           </span>`
        : `<span class="sprint-item-actions">${meta}</span>`;
      return `<div class="sprint-item-row" data-item="${escapeHtml(it.id)}">
        <span class="sprint-item-icon" style="color:${color}">${icon}</span>
        <span class="sprint-item-title">${escapeHtml(it.title)}</span>
        <span class="sprint-item-ver">${escapeHtml(it.version)}</span>
        ${actions}
      </div>`;
    }).join('');
  }

  // Footer: progress bar + add input
  const total = items.length;
  const done = items.filter(i => i.status === 'done').length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  html += `<div class="sprint-footer">
    <span class="sprint-pct">${done}/${total} · ${pct}%</span>
    <div class="sprint-add-row">
      <input class="live-add-input" id="sprint-add-input-${projectId}"
             placeholder="version:title  or  just title" style="flex:1">
      <button class="secondary sprint-add-btn" data-pid="${escapeHtml(projectId)}"
              style="margin-left:4px">+ Add</button>
    </div>
  </div>`;
  root.innerHTML = html;
  root.querySelector('.sprint-add-btn').onclick =
    () => addSprintItemFromInput(projectId);
  wireSprintAddEnter(projectId, root);
}

function wireSprintAddEnter(projectId, root) {
  /** Allow Enter in the sprint-add input to submit. */
  const inp = root.querySelector(`#sprint-add-input-${projectId}`);
  if (inp) inp.onkeydown = e => { if (e.key === 'Enter') addSprintItemFromInput(projectId); };
}

async function sprintAction(projectId, itemId, action) {
  /** POST to one of the sprint-item action endpoints. */
  try {
    await api(`/projects/${projectId}/sprint-items/${itemId}/${action}`,
      { method: 'POST', body: JSON.stringify({}) });
    toast(`Sprint item ${action}d`);
    await refreshLiveTab(projectId);
  } catch(e) { toast(`Failed: ${e.message}`, true); }
}

async function sprintPushPrompt(projectId, itemId) {
  /** Prompt for a target version then push the item. */
  const toVersion = window.prompt('Push to version (e.g. v2.0):');
  if (!toVersion) return;
  try {
    await api(`/projects/${projectId}/sprint-items/${itemId}/push`,
      { method: 'POST', body: JSON.stringify({ to_version: toVersion }) });
    toast('Sprint item pushed to ' + toVersion);
    await refreshLiveTab(projectId);
  } catch(e) { toast(`Push failed: ${e.message}`, true); }
}

async function addSprintItemFromInput(projectId) {
  /** Parse "version:title" or fall back to current sprint version. */
  const inp = document.getElementById(`sprint-add-input-${projectId}`);
  if (!inp) return;
  const val = inp.value.trim();
  if (!val) return;
  let version, title;
  const colonIdx = val.indexOf(':');
  if (colonIdx > 0) {
    version = val.slice(0, colonIdx).trim();
    title   = val.slice(colonIdx + 1).trim();
  } else {
    // Fall back to the sprint header text as a rough version token.
    const panel = state.panels[projectId];
    const sprint = (panel && panel._serverSprint) || '';
    const m = sprint.match(/v[\d.x]+/i);
    version = m ? m[0] : 'current';
    title = val;
  }
  if (!title) { toast('Title required', true); return; }
  try {
    await api(`/projects/${projectId}/sprint-items`, {
      method: 'POST',
      body: JSON.stringify({ version, title }),
    });
    inp.value = '';
    toast('Sprint item added');
    await refreshLiveTab(projectId);
  } catch(e) { toast('Add failed: ' + e.message, true); }
}

function cacheMostRecentSession(projectId, sessions) {
  /** Pick the most recent active session id for "add task" attribution. */
  const panel = state.panels[projectId];
  if (!panel) return;
  const sorted = sessions.slice().sort((a, b) =>
    (b.last_seen || '').localeCompare(a.last_seen || '')
  );
  const top = sorted.find(s => s.status !== 'closed') || sorted[0];
  if (top) panel.liveLastSessionId = top.id;
}

function renderLiveSessions(projectId, sessions, tasks) {
  const root = document.getElementById(`live-sessions-${projectId}`);
  if (!root) return;
  const now = Date.now();
  const claimMap = new Map();
  tasks.forEach(t => {
    if (t.claimed_by && (t.status === 'pending' || t.status === 'in_progress')) {
      claimMap.set(t.claimed_by, t);
    }
  });
  const rows = sessions
    .map(s => {
      let ageMs = 0;
      try {
        const ts = s.last_seen ? s.last_seen.replace(' ', 'T') + 'Z' : '';
        if (ts) ageMs = now - new Date(ts).getTime();
      } catch(e) {}
      return { s, ageMs };
    })
    .filter(({ ageMs }) => ageMs > 0 && ageMs <= 24 * 3600 * 1000)
    .sort((a, b) => a.ageMs - b.ageMs);
  if (!rows.length) {
    root.innerHTML = '<div class="live-empty">No active sessions.</div>';
    return;
  }
  root.innerHTML = rows.map(({ s, ageMs }) => {
    const mins = ageMs / 60000;
    const dot = mins < 5 ? '🟢' : mins < 30 ? '🟡' : '⚫';
    const label = s.human_id ? `${s.human_id}/${s.name}` : s.name;
    const claimed = claimMap.get(s.id);
    const claimedRow = claimed
      ? `<div class="live-session-task">↳ ${escapeHtml((claimed.description || '').slice(0, 140))}</div>`
      : '';
    const summary = s.session_summary;
    const summaryRow = (summary && summary.summary && (s.status === 'closed' || s.status === 'archived'))
      ? `<div class="live-session-outcome" style="font-size:10px;color:var(--muted);margin-top:3px;padding-left:18px">`
        + `✓ ${escapeHtml((summary.summary || '').slice(0, 160))}`
        + (summary.tasks_completed != null ? ` · ${summary.tasks_completed} tasks` : '')
        + `</div>`
      : '';
    return `<div class="live-session-row">
      <div class="live-session-head">
        <span class="live-dot">${dot}</span>
        <span class="live-session-name">${escapeHtml(label)}</span>
        <span class="live-session-age">${escapeHtml(formatRelativeTime(s.last_seen))}</span>
      </div>
      ${claimedRow}${summaryRow}
    </div>`;
  }).join('');
}

function renderLiveQueue(projectId, tasks) {
  const root = document.getElementById(`live-queue-${projectId}`);
  if (!root) return;
  const live = tasks.filter(t => t.status === 'pending' || t.status === 'in_progress');
  if (!live.length) {
    root.innerHTML = '<div class="live-empty">Queue is empty. Add a task above.</div>';
    return;
  }
  live.sort((a, b) => {
    if (a.status !== b.status) return a.status === 'in_progress' ? -1 : 1;
    return (b.created_at || '').localeCompare(a.created_at || '');
  });
  root.innerHTML = live.map(t => {
    const dot = t.status === 'in_progress' ? '🔵' : '📋';
    const claimLabel = t.claimed_by_human_id || t.claimed_by_session_name || t.claimed_by || '';
    const claimed = t.claimed_by
      ? `<span class="live-task-claim">claimed by: ${escapeHtml(claimLabel.slice(0, 24))}</span>`
      : '';
    const ts = formatRelativeTime(t.created_at);
    const eid = `live-expand-${projectId}-${t.id.slice(0, 8)}`;
    const expandMeta = [
      t.session_name ? `session: ${t.session_name}` : '',
      t.claimed_by   ? `claimed_by: ${t.claimed_by}` : '',
      t.created_at   ? `created: ${t.created_at}` : '',
      t.claimed_at   ? `claimed: ${t.claimed_at}` : '',
    ].filter(Boolean).join(' · ');
    return `<div class="live-task-row" data-task="${escapeHtml(t.id)}">
      <span class="live-dot">${dot}</span>
      <div class="live-task-body" style="cursor:pointer" onclick="toggleExpand('${eid}')">
        <div class="live-task-desc">${escapeHtml((t.description || '').slice(0, 200))}</div>
        <div class="live-task-meta">${escapeHtml(ts)} ${claimed} <span class="expand-arrow" style="font-size:9px;color:var(--muted)">▶</span></div>
        <div id="${eid}" style="display:none;margin-top:4px;font-size:10px;color:var(--muted);white-space:pre-wrap;word-break:break-word">${escapeHtml(t.description || '')}${expandMeta ? '\n' + escapeHtml(expandMeta) : ''}</div>
      </div>
      <button class="live-task-cancel" data-cancel="${escapeHtml(t.id)}" title="Mark done / cancel">×</button>
    </div>`;
  }).join('');
  root.querySelectorAll('button[data-cancel]').forEach(btn => {
    btn.onclick = () => cancelLiveTask(projectId, btn.dataset.cancel);
  });
}

async function addLiveTask(projectId, description) {
  /** POST /tasks with the most recent active session as attribution. */
  const panel = state.panels[projectId];
  const sessionId = panel && panel.liveLastSessionId;
  if (!sessionId) {
    // Try to discover one synchronously.
    try {
      const sessions = await api(`/projects/${projectId}/sessions`);
      cacheMostRecentSession(projectId, sessions || []);
    } catch(e) {}
  }
  const sid = panel && panel.liveLastSessionId;
  if (!sid) {
    toast('No active session to attribute the task to', true);
    return false;
  }
  try {
    await api('/tasks', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sid, project_id: projectId,
        description, status: 'pending',
      }),
    });
    toast('task queued');
    await refreshLiveTab(projectId);
    return true;
  } catch(e) {
    toast('add task failed: ' + e.message, true);
    return false;
  }
}

async function cancelLiveTask(projectId, taskId) {
  /** PATCH /tasks/{id} → status=done. WebSocket broadcast triggers refresh. */
  try {
    await api(`/tasks/${taskId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'done' }),
    });
    toast('task closed');
    await refreshLiveTab(projectId);
  } catch(e) {
    toast('cancel failed: ' + e.message, true);
  }
}

// v1.5.x — Claude launch control panel. Wires the 4 sections:
// (1) continue session dropdown + copy resume command
// (2) start worker → show XML → copy
// (3) handoff copy + regenerate
// (4) open in claude.ai
function wireClaudeLaunchPanel(projectId) {
  const PROJECT_QUOTE = projectId.replace(/"/g, '\\"');

  // Section 1 — Copy resume command
  const copyResumeBtn = document.getElementById(`copy-resume-${projectId}`);
  if (copyResumeBtn) copyResumeBtn.onclick = async () => {
    const sel = document.getElementById(`continue-session-${projectId}`);
    const sessionName = sel && sel.value ? sel.value : '';
    if (!sessionName) { toast('pick a session first', true); return; }
    const cmd = `start_session(project_id="${PROJECT_QUOTE}", session_name="${sessionName.replace(/"/g, '\\"')}", human_id="adam")`;
    try {
      await navigator.clipboard.writeText(cmd);
      toast('resume command copied');
    } catch(e) { toast('copy failed: ' + e.message, true); }
  };

  // Section 2 — Start Worker
  const startWorkerBtn = document.getElementById(`start-worker-${projectId}`);
  if (startWorkerBtn) startWorkerBtn.onclick = async () => {
    const resultEl = document.getElementById(`worker-result-${projectId}`);
    const emptyEl = document.getElementById(`worker-empty-${projectId}`);
    const xmlEl = document.getElementById(`worker-xml-${projectId}`);
    if (resultEl) resultEl.style.display = 'none';
    if (emptyEl) emptyEl.style.display = 'none';
    try {
      const r = await fetch(`/projects/${projectId}/start-worker-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (r.status === 404) {
        if (emptyEl) emptyEl.style.display = '';
        return;
      }
      if (!r.ok) throw new Error(`${r.status}`);
      const body = await r.json();
      const xml = body.worker_context || '';
      if (xmlEl) xmlEl.textContent = xml;
      if (resultEl) resultEl.style.display = '';
      toast('worker session ready');
    } catch(e) { toast('start worker failed: ' + e.message, true); }
  };
  const copyWorkerBtn = document.getElementById(`copy-worker-${projectId}`);
  if (copyWorkerBtn) copyWorkerBtn.onclick = async () => {
    const xmlEl = document.getElementById(`worker-xml-${projectId}`);
    const text = xmlEl ? xmlEl.textContent : '';
    if (!text) { toast('nothing to copy', true); return; }
    try {
      await navigator.clipboard.writeText(text);
      toast('worker context copied');
    } catch(e) { toast('copy failed: ' + e.message, true); }
  };

  // Section 3 — Handoff copy + regenerate
  const copyHandoffBtn = document.getElementById(`copy-handoff-${projectId}`);
  if (copyHandoffBtn) copyHandoffBtn.onclick = async () => {
    // v2.3 — Code Handoff now copies the compact context block (north star,
    // sprint, pending items, recent tasks, decisions, sessions). The legacy
    // verbose handoff file is still written via the Regenerate button.
    try {
      const r = await fetch(`/projects/${projectId}/context-block?mode=full`);
      if (!r.ok) throw new Error(`${r.status}`);
      const text = await r.text();
      if (text) {
        try {
          await navigator.clipboard.writeText(text);
          toast('handoff copied — paste into Claude');
        } catch(_) {
          // Clipboard blocked — show in modal for manual copy
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:80vw;height:60vh;z-index:9999;font-family:monospace;font-size:11px;background:#1a1a2e;color:#e0e0e0;border:2px solid var(--accent);border-radius:6px;padding:12px';
          const overlay = document.createElement('div');
          overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9998';
          const closeBtn = document.createElement('button');
          closeBtn.textContent = 'Close';
          closeBtn.style.cssText = 'position:fixed;top:calc(50% - 31vh);left:calc(50% + 38vw);z-index:10000;background:var(--accent);border:none;color:white;padding:6px 14px;border-radius:4px;cursor:pointer';
          document.body.appendChild(overlay);
          document.body.appendChild(ta);
          document.body.appendChild(closeBtn);
          ta.select();
          const close = () => { overlay.remove(); ta.remove(); closeBtn.remove(); };
          overlay.onclick = close;
          closeBtn.onclick = close;
          toast('Clipboard blocked — select all and copy manually', true);
        }
      } else {
        toast('handoff written: ' + (body.path || 'data/'), false);
      }
      stampHandoffTs(projectId, new Date());
    } catch(e) { toast('handoff failed: ' + e.message, true); }
  };
  const copyContextBtn = document.getElementById(`copy-context-${projectId}`);
  if (copyContextBtn) copyContextBtn.onclick = async () => {
    // v2.3 — fetches the shorter ?mode=chat plain-text context block so
    // a paste into a new claude.ai chat fits without overflow.
    const orig = copyContextBtn.textContent;
    copyContextBtn.disabled = true;
    copyContextBtn.textContent = 'Loading…';
    try {
      const r = await fetch(`/projects/${projectId}/context-block?mode=chat`);
      if (!r.ok) throw new Error(`${r.status}`);
      const text = await r.text();
      await navigator.clipboard.writeText(text);
      copyContextBtn.textContent = 'Copied ✓';
      setTimeout(() => { copyContextBtn.textContent = orig; }, 2000);
      toast('chat context copied');
    } catch(e) { toast('copy context failed: ' + e.message, true); }
    finally { copyContextBtn.disabled = false; }
  };

  const regenBtn = document.getElementById(`regen-handoff-${projectId}`);
  if (regenBtn) regenBtn.onclick = async () => {
    const tsEl = document.getElementById(`handoff-ts-${projectId}`);
    const orig = regenBtn.textContent;
    regenBtn.disabled = true;
    regenBtn.textContent = 'Regenerating…';
    try {
      const r = await fetch(`/projects/${projectId}/handoff`, { method: 'POST' });
      if (!r.ok) throw new Error(`${r.status}`);
      await r.json();
      stampHandoffTs(projectId, new Date());
      if (tsEl) {
        const prev = tsEl.textContent;
        tsEl.textContent = 'Regenerated ✓';
        setTimeout(() => stampHandoffTs(projectId, new Date()), 2000);
      }
      toast('handoff regenerated');
    } catch(e) { toast('regenerate failed: ' + e.message, true); }
    finally {
      regenBtn.disabled = false;
      regenBtn.textContent = orig;
    }
  };
}

function stampHandoffTs(projectId, when) {
  const tsEl = document.getElementById(`handoff-ts-${projectId}`);
  if (!tsEl) return;
  const iso = when.toISOString().replace('T', ' ').slice(0, 19);
  tsEl.textContent = 'Last generated: ' + formatRelativeTime(iso);
}

function populateSessionDropdown(projectId, sessions) {
  /** v1.5.x — fill the "Continue session" dropdown with the last 5 sessions
   * (newest first by last_seen). Each option label: "{name} — {age} ago". */
  const sel = document.getElementById(`continue-session-${projectId}`);
  if (!sel) return;
  const sorted = (sessions || []).slice().sort((a, b) =>
    (b.last_seen || '').localeCompare(a.last_seen || '')
  ).slice(0, 5);
  if (!sorted.length) {
    sel.innerHTML = '<option value="">(no sessions yet)</option>';
    return;
  }
  const prev = sel.value;
  sel.innerHTML = sorted.map(s => {
    const label = `${s.name} — ${formatRelativeTime(s.last_seen)}`;
    return `<option value="${escapeHtml(s.name)}">${escapeHtml(label)}</option>`;
  }).join('');
  if (prev && sorted.some(s => s.name === prev)) sel.value = prev;
}

// v1.1.1 — Activity Timeline. Load /timeline, lay out a swimlane
// per session, paint task pills positioned on a shared time axis.
async function loadTimeline(projectId) {
  const wrap = document.getElementById(`timeline-wrap-${projectId}`);
  if (!wrap) return;
  wrap.innerHTML = `<div class="timeline-empty">loading…</div>`;
  let data;
  try {
    data = await api(`/projects/${projectId}/timeline`);
  } catch (e) {
    wrap.innerHTML = `<div class="timeline-empty">timeline failed: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderTimeline(projectId, data);
  const axisBtn = document.getElementById(`timeline-axis-${projectId}`);
  if (axisBtn) axisBtn.onclick = () => {
    const p = state.panels[projectId];
    p._timelineAbsolute = !p._timelineAbsolute;
    axisBtn.textContent = p._timelineAbsolute ? 'absolute' : 'relative';
    renderTimeline(projectId, data);
  };
  const refreshBtn = document.getElementById(`timeline-refresh-${projectId}`);
  if (refreshBtn) refreshBtn.onclick = () => loadTimeline(projectId);
}

function renderTimeline(projectId, data) {
  const wrap = document.getElementById(`timeline-wrap-${projectId}`);
  if (!wrap) return;
  const { tasks = [], sessions = [], goal_events = [] } = data || {};
  if (!sessions.length && !tasks.length && !goal_events.length) {
    wrap.innerHTML = `<div class="timeline-empty">no activity yet — log a task to see it here</div>`;
    return;
  }
  const isAbs = !!(state.panels[projectId] && state.panels[projectId]._timelineAbsolute);
  const fmtTs = (ts) => {
    if (!ts) return '';
    const iso = ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z';
    return isAbs
      ? new Date(iso).toISOString().replace('T', ' ').slice(0, 16)
      : formatRelativeTime(ts);
  };

  // Build a unified event list; sort newest first.
  const events = [];
  tasks.forEach(t => {
    const icon = { done: '✅', failed: '❌', pending: '⏳', in_progress: '🔄' }[t.status] || '•';
    events.push({
      ts: t.created_at,
      actor: t.session_name || '(unknown)',
      desc: `${icon} ${t.description.slice(0, 100)}`,
    });
  });
  sessions.forEach(s => {
    const label = s.human_id ? `${s.human_id}/${s.name}` : s.name;
    events.push({
      ts: s.registered_at || s.last_seen || '',
      actor: label,
      desc: `🟢 session ${s.status || 'registered'}`,
    });
  });
  // Collapse goal events — show only latest per field per hour
  const goalByField = new Map();
  goal_events.forEach(g => {
    // Skip version_goal events that are ONLY an AUTO BLOCKS update (minor=True writes)
    // These are identified by new_summary starting with AUTO SUMMARY or containing only auto-block content
    if (g.field === 'version_goal') {
      const summary = (g.new_summary || '');
      if (summary.startsWith('[AUTO SUMMARY') || summary.startsWith('- [DONE]') || summary.startsWith('- [PENDING]')) return;
    }
    const key = g.field + (g.updated_at || '').slice(0, 13); // group by field+hour
    if (!goalByField.has(key) || g.version > (goalByField.get(key).version || 0)) {
      goalByField.set(key, g);
    }
  });
  goalByField.forEach(g => {
    events.push({ ts: g.updated_at || '', actor: 'goal', desc: `📋 ${g.field} → v${g.version}` });
  });
  events.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));

  const rows = events.map(e =>
    `<div class="timeline-log-entry">` +
    `<span class="timeline-log-ts">${escapeHtml(fmtTs(e.ts))}</span>` +
    `<span class="timeline-log-actor">${escapeHtml(e.actor)}</span>` +
    `<span class="timeline-log-desc">${escapeHtml(e.desc)}</span>` +
    `</div>`
  ).join('');
  wrap.innerHTML = `<div class="timeline-log">${rows}</div>`;
}

async function loadQueue(projectId) {
  /**v1.4.0 — work queue panel. Loads all tasks and segments them into
   * pending / in_progress / done / failed buckets — a Minecraft-hopper view
   * of the task flow: items arrive pending, get claimed in_progress, complete
   * to done or fail. Newest entries appear first within each bucket. */
  const body = document.getElementById(`queue-body-${projectId}`);
  if (!body) return;
  body.innerHTML = '<div class="empty" style="color:var(--muted)">loading…</div>';
  try {
    const tasks = await api(`/projects/${projectId}/tasks?limit=100`);
    body.innerHTML = renderQueue(tasks);
    const refreshBtn = document.getElementById(`queue-refresh-${projectId}`);
    if (refreshBtn) refreshBtn.onclick = () => loadQueue(projectId);
  } catch (e) {
    body.innerHTML = `<div class="empty">queue failed: ${escapeHtml(e.message)}</div>`;
  }
}

function renderQueue(tasks) {
  /**Segment tasks into pipeline stages and render each as a collapsible section. */
  const pending = tasks.filter(t => t.status === 'pending');
  const inProg = tasks.filter(t => t.status === 'in_progress');
  const done = tasks.filter(t => t.status === 'done').slice(0, 10);
  const failed = tasks.filter(t => t.status === 'failed').slice(0, 10);
  const backlog = tasks.filter(t => t.status === 'backlog');
  const future = tasks.filter(t => t.status === 'future');

  const sect = (icon, title, items, emptyMsg, showActions) => {
    const rows = items.length
      ? items.map(t => {
          const who = t.human_id || t.session_name || '';
          const sessLine = '';
          const tsLine = t.created_at
            ? `<span class="queue-item-ts">${escapeHtml((who ? who + ' · ' : '') + formatRelativeTime(t.created_at))}</span>` : '';
          const eid = `queue-expand-${t.id.slice(0, 8)}`;
          const expandMeta = [
            t.human_id     ? `human: ${t.human_id}` : '',
            t.session_name ? `session: ${t.session_name}` : '',
            t.claimed_by   ? `claimed_by: ${t.claimed_by_human_id || t.claimed_by_session_name || t.claimed_by}` : '',
            t.created_at   ? `created: ${t.created_at}` : '',
            t.claimed_at   ? `claimed: ${t.claimed_at}` : '',
          ].filter(Boolean).join(' · ');
          const actions = showActions ? `<div style="display:flex;gap:4px;margin-top:4px" onclick="event.stopPropagation()">
            ${t.status === 'pending' ? `<button onclick="_queueAction('${escapeHtml(t.id)}','backlog')" style="background:none;border:1px solid var(--border);color:var(--muted);font-size:9px;padding:2px 6px;border-radius:3px;cursor:pointer;font-family:var(--font-mono)" title="Push to backlog">📦 backlog</button>` : ''}
            ${t.status === 'pending' ? `<button onclick="_queueAction('${escapeHtml(t.id)}','done')" style="background:none;border:1px solid var(--status-done);color:var(--status-done);font-size:9px;padding:2px 6px;border-radius:3px;cursor:pointer;font-family:var(--font-mono)" title="Mark done">✓ done</button>` : ''}
            <button onclick="_queueAction('${escapeHtml(t.id)}','delete')" style="background:none;border:1px solid var(--border);color:var(--status-failed);font-size:9px;padding:2px 6px;border-radius:3px;cursor:pointer;font-family:var(--font-mono)" title="Delete task">✕ delete</button>
          </div>` : '';
          return `<div class="queue-item" style="cursor:pointer" onclick="toggleExpand('${eid}')">
            ${escapeHtml((t.description || '').slice(0, 120))}${sessLine}${tsLine}
            <span class="expand-arrow" style="font-size:9px;color:var(--muted);margin-left:4px">▶</span>
            <div id="${eid}" style="display:none;margin-top:4px;font-size:10px;color:var(--muted);white-space:pre-wrap;word-break:break-word">${escapeHtml(t.description || '')}${expandMeta ? '\n' + escapeHtml(expandMeta) : ''}${actions}</div>
          </div>`;
        }).join('')
      : `<div class="queue-empty">${emptyMsg}</div>`;
    return `<div class="queue-section">` +
      `<div class="queue-section-header">${icon} ${title} <span style="color:var(--accent)">(${items.length})</span></div>` +
      rows + `</div>`;
  };

  return sect('⏳', 'Pending', pending, 'no pending tasks', true) +
         sect('🔄', 'In Progress', inProg, 'nothing running', false) +
         sect('✅', 'Recently Done', done, 'no completed tasks', true) +
         (failed.length ? sect('❌', 'Failed', failed, '', true) : '') +
         (backlog.length ? sect('📦', 'Backlog', backlog, '', true) : '') +
         (future.length ? sect('🔮', 'Future', future, '', false) : '');
}

// Global queue action handler — called from inline onclick in renderQueue
window._queueAction = async function(taskId, action) {
  try {
    if (action === 'delete') {
      if (!confirm('Delete this task?')) return;
      await api(`/tasks/${taskId}`, { method: 'DELETE' });
    } else if (action === 'done') {
      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) });
    } else if (action === 'backlog') {
      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'backlog' }) });
    }
    // Reload all queue panels
    document.querySelectorAll('[id^="queue-body-"]').forEach(el => {
      const pid = el.id.replace('queue-body-', '');
      loadQueue(pid);
    });
  } catch(e) { toast('Action failed: ' + e.message, true); }
};

async function loadFilesTab(projectId) {
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
    listEl.innerHTML = files.map(f =>
      `<div class="file-item" data-filename="${escapeHtml(f)}">${escapeHtml(f)}</div>`
    ).join('');
    listEl.querySelectorAll('.file-item').forEach(item => {
      item.onclick = () => openFileEditor(projectId, item.dataset.filename);
    });
  } catch(e) {
    listEl.innerHTML = `<div style="padding:14px;color:var(--status-failed);font-family:'IBM Plex Mono',monospace;font-size:11px">Error: ${escapeHtml(e.message)}</div>`;
  }
}

async function openFileEditor(projectId, filename) {
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
      previewDiv.style.display = '';
    }
    contentEl.style.display = 'none';
    // Wire edit/preview toggle if not already wired
    if (editBtn && !editBtn._wired) {
      editBtn._wired = true;
      [editBtn, previewBtn].forEach(btn => {
        btn.onclick = () => {
          [editBtn, previewBtn].forEach(b => b.classList.toggle('active', b === btn));
          if (btn.dataset.fmode === 'preview') {
            const md = contentEl.value || '';
            const html = (typeof marked !== 'undefined') ? marked.parse(md) : escapeHtml(md);
            previewDiv.innerHTML = html;
            contentEl.style.display = 'none';
            previewDiv.style.display = '';
          } else {
            previewDiv.style.display = 'none';
            contentEl.style.display = '';
          }
        };
      });
    }
  } catch(e) { toast('open failed: ' + e.message, true); }
}

async function saveFile(projectId) {
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
  } catch(e) { toast('save failed: ' + e.message, true); }
}

async function refreshTab(projectId) {
  await Promise.all([
    refreshGoal(projectId),
    refreshSessions(projectId),
    refreshTasks(projectId),
  ]);
}

async function refreshGoal(projectId) {
  const ta = document.getElementById(`goal-${projectId}`);
  const v = document.getElementById(`goal-version-${projectId}`);
  if (!ta) return;
  try {
    const goal = await api(`/projects/${projectId}/goal`);
    state.panels[projectId].goalRaw = goal.content;
    let text;
    if (typeof goal.content === 'string') {
      state.panels[projectId].goalIsJson = false;
      text = goal.content;
    } else {
      state.panels[projectId].goalIsJson = true;
      text = JSON.stringify(goal.content, null, 2);
    }
    // Split out title line + AUTO BLOCKS zone
    const AUTO_SPLIT = '--- AUTO BLOCKS BELOW ---';
    const splitIdx = text.indexOf(AUTO_SPLIT);
    const mainText = splitIdx !== -1 ? text.slice(0, splitIdx).trimEnd() : text;

    // Zone 1: title (line 0) — read-only
    // Zone 2: SHIPPED block — read-only  
    // Zone 3: CURRENT FOCUS onwards — editable
    const allLines = mainText.split('\n');
    const titleLine = allLines[0] || '';
    const titleEl = document.getElementById(`goal-title-${projectId}`);
    if (titleEl) titleEl.textContent = titleLine;

    const body = allLines.slice(1).join('\n').replace(/^\n/, '');
    // Find CURRENT FOCUS as the start of editable zone
    const editStart = body.search(/^(CURRENT FOCUS|KEY FILES)/m);
    if (editStart > 0) {
      const shippedEl = document.getElementById(`goal-shipped-${projectId}`);
      if (shippedEl) {
        shippedEl.style.display = 'block';
        shippedEl.textContent = body.slice(0, editStart).trimEnd();
      }
      ta.value = body.slice(editStart);
    } else {
      const shippedEl = document.getElementById(`goal-shipped-${projectId}`);
      if (shippedEl) shippedEl.style.display = 'none';
      ta.value = body;
    }

    const autoBlocksEl = document.getElementById(`goal-autoblocks-${projectId}`);
    if (autoBlocksEl) {
      if (splitIdx !== -1) {
        const abWrapper = document.getElementById(`goal-autoblocks-wrapper-${projectId}`);
        if (abWrapper) abWrapper.style.display = 'block';
        autoBlocksEl.style.display = 'block';
        autoBlocksEl.textContent = text.slice(splitIdx + '--- AUTO BLOCKS BELOW ---'.length).trimStart();
      } else {
        const abWrapper2 = document.getElementById(`goal-autoblocks-wrapper-${projectId}`);
        if (abWrapper2) abWrapper2.style.display = 'none';
        autoBlocksEl.style.display = 'none';
      }
    }
    v.textContent = `v${goal.version}`;
    // v0.5.2 — north star and sprint textareas
    const nsTA = document.getElementById(`goal-north-star-${projectId}`);
    const spTA = document.getElementById(`goal-sprint-${projectId}`);
    if (nsTA) nsTA.value = goal.north_star || '';
    if (spTA) spTA.value = goal.sprint || '';
    // v0.6.4 — store server values for dirty tracking; clear dirty state
    const p = state.panels[projectId];
    p._serverNorthStar = goal.north_star || '';
    p._serverSprint = goal.sprint || '';
    p._lastSaved = text;
    if (nsTA) { nsTA.classList.remove('dirty'); }
    if (spTA) { spTA.classList.remove('dirty'); }
    ta.classList.remove('dirty');
    // v0.6.4 — last-modified timestamps
    const tsNs = document.getElementById(`goal-ns-ts-${projectId}`);
    const tsVg = document.getElementById(`goal-vg-ts-${projectId}`);
    const tsSp = document.getElementById(`goal-sp-ts-${projectId}`);
    const updAt = goal.updated_at ? formatRelativeTime(goal.updated_at) : '';
    if (tsNs) tsNs.textContent = updAt ? `· ${updAt}` : '';
    if (tsVg) tsVg.textContent = updAt ? `· ${updAt}` : '';
    if (tsSp) tsSp.textContent = updAt ? `· ${updAt}` : '';
    // v2.3 — decisions table subtab. goal.decisions is the append-only
    // blob: "[YYYY-MM-DD] text\n\n[YYYY-MM-DD] text\n\n..." (newest first).
    renderDecisionsTable(projectId, goal.decisions || '');
  } catch (e) {
    ta.value = '';
    v.textContent = '(unset)';
  }
}

function parseDecisionsBlob(blob) {
  /** v2.3 — split the append-only decisions log into [{date, text}] rows.
   *
   * Format: "[YYYY-MM-DD] text...\n\n[YYYY-MM-DD] more text...\n\n"
   * Entries without a bracketed date keep `date: ''` and show their full
   * text (so legacy / malformed rows are still visible, not dropped).
   * Newest first — preserves the on-disk ordering.
   */
  if (!blob || typeof blob !== 'string') return [];
  const chunks = blob.split(/\n\s*\n/).map(c => c.trim()).filter(Boolean);
  return chunks.map(chunk => {
    const m = chunk.match(/^\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$/s);
    if (m) return { date: m[1], text: m[2].trim() };
    return { date: '', text: chunk };
  });
}

function renderDecisionsTable(projectId, blob) {
  /** v2.3 — render the decisions blob as a proper accent-bordered table.
   *
   * Read-only: there is no edit affordance here. Decisions are append-only
   * via the `set_decision` MCP tool. The accent border-left on each row
   * matches the visual language of recent-tasks and sprint cards.
   */
  const host = document.getElementById(`decisions-table-${projectId}`);
  if (!host) return;
  const rows = parseDecisionsBlob(blob);
  if (rows.length === 0) {
    host.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">(no decisions logged yet — call <code>set_decision</code> from MCP to record one)</div>`;
    return;
  }
  const html = rows.map(row => {
    const date = row.date ? escapeHtml(row.date) : '—';
    // Text may include long single-line decisions; preserve whitespace
    // (set_decision lets users embed paragraphs) but allow wrap.
    return `<div style="display:grid;grid-template-columns:96px 1fr;gap:10px;padding:8px 10px;border-left:3px solid var(--accent);background:var(--surface-2);border-radius:0 4px 4px 0;margin-bottom:6px;align-items:start">
      <div style="color:var(--accent);font-weight:600;white-space:nowrap">${date}</div>
      <div style="color:var(--text);white-space:pre-wrap;word-break:break-word;line-height:1.5">${escapeHtml(row.text)}</div>
    </div>`;
  }).join('');
  host.innerHTML = html;
}

function wireGoalPreviewToggle(taEl, previewEl) {
  /**v1.4.1 — repurposed as generic edit/preview toggle for a single
   * textarea + preview div pair.  Used by the Files tab editor.
   * Old per-goal-field wiring removed — goal fields are structured data,
   * not markdown documents.  Called as wireGoalPreviewToggle(taEl, prevEl). */
  if (!taEl || !previewEl) return;
  const row = document.createElement('div');
  row.className = 'preview-toggle-row';
  row.innerHTML =
    `<button class="preview-btn active" data-mode="edit">edit</button>` +
    `<button class="preview-btn"        data-mode="preview">preview</button>`;
  taEl.parentNode.insertBefore(row, taEl);
  row.querySelectorAll('.preview-btn').forEach(btn => {
    btn.onclick = () => {
      const mode = btn.dataset.mode;
      row.querySelectorAll('.preview-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.mode === mode);
      });
      if (mode === 'preview') {
        const md = taEl.value || '';
        const html = (typeof marked !== 'undefined')
          ? marked.parse(md)
          : escapeHtml(md);
        previewEl.innerHTML = html;
        taEl.style.display = 'none';
        previewEl.style.display = '';
      } else {
        previewEl.style.display = 'none';
        taEl.style.display = '';
      }
    };
  });
}

async function saveGoal(projectId) {
  const ta = document.getElementById(`goal-${projectId}`);
  if (!ta) return;
  // Reattach auto blocks before saving so they aren't lost
  const autoBlocksEl = document.getElementById(`goal-autoblocks-${projectId}`);
  const autoBlocksText = (autoBlocksEl && autoBlocksEl.style.display !== 'none')
    ? '\n--- AUTO BLOCKS BELOW ---\n' + autoBlocksEl.textContent : '';
  const titleEl = document.getElementById(`goal-title-${projectId}`);
  const titleLine = (titleEl && titleEl.textContent) ? titleEl.textContent + '\n' : '';
  const shippedEl2 = document.getElementById(`goal-shipped-${projectId}`);
  const shippedText = (shippedEl2 && shippedEl2.style.display !== 'none' && shippedEl2.textContent)
    ? '\n' + shippedEl2.textContent + '\n' : '';
  const raw = titleLine + shippedText + ta.value + autoBlocksText;
  if (raw === state.panels[projectId]._lastSaved) return;
  let content = raw;
  if (state.panels[projectId].goalIsJson) {
    try { content = JSON.parse(raw); } catch(e) { /* fall back to string */ }
  }
  try {
    await api(`/projects/${projectId}/goal`, { method: 'POST', body: JSON.stringify({ content }) });
    state.panels[projectId]._lastSaved = raw;
    toast('version goal saved');
    refreshGoal(projectId);
  } catch (e) {
    toast('save failed: ' + e.message, true);
  }
}

async function saveNorthStar(projectId) {
  const ta = document.getElementById(`goal-north-star-${projectId}`);
  if (!ta) return;
  const val = ta.value.trim();
  if (!val) return;
  try {
    const humanInput = document.getElementById('new-project-human');
    const humanId = humanInput ? humanInput.value.trim() : '';
    await api(`/projects/${projectId}/goal/north-star`, {
      method: 'POST',
      body: JSON.stringify({ north_star: val, human_id: humanId || 'owner' }),
    });
    toast('north star saved');
    refreshGoal(projectId);
  } catch (e) {
    toast('save failed: ' + e.message, true);
  }
}

async function saveSprint(projectId) {
  const ta = document.getElementById(`goal-sprint-${projectId}`);
  if (!ta) return;
  const val = ta.value.trim();
  if (!val) return;
  try {
    await api(`/projects/${projectId}/goal/sprint`, {
      method: 'POST',
      body: JSON.stringify({ sprint: val }),
    });
    toast('sprint saved');
    refreshGoal(projectId);
  } catch (e) {
    toast('save failed: ' + e.message, true);
  }
}

async function refreshSessions(projectId) {
  const root = document.getElementById(`sessions-${projectId}`);
  if (!root) return;
  try {
    const sessions = await api(`/projects/${projectId}/sessions`);
    populateSessionDropdown(projectId, sessions);
    root.innerHTML = sessions.map(s => {
      // v1.4.1 — dim stale sessions: active (<1h) full, idle (1-24h) 70%, old (24h+) 40%
      let ageMs = 0;
      try {
        const ts = s.last_seen ? s.last_seen.replace(' ', 'T') + 'Z' : '';
        if (ts) ageMs = Date.now() - new Date(ts).getTime();
      } catch(e) {}
      const ageH = ageMs / 3_600_000;
      const opacity = ageH < 1 ? 1 : ageH < 24 ? 0.7 : 0.4;
      const fontStyle = ageH >= 24 ? 'italic' : 'normal';
      const label = s.human_id ? `${s.human_id}/${s.name}` : s.name;
      return `<div class="session-row" style="opacity:${opacity};font-style:${fontStyle}">` +
        `<span class="name">${escapeHtml(label)}</span>` +
        `<span class="meta">${escapeHtml(s.status)} · ${escapeHtml(formatRelativeTime(s.last_seen))}</span>` +
        `</div>`;
    }).join('') || '<div class="session-row meta">(no active sessions)</div>';
  } catch(e) {}
}

async function refreshTasks(projectId) {
  try {
    const tasks = await api(`/projects/${projectId}/tasks?limit=50`);
    state.panels[projectId].taskCache = tasks;
    renderTasks(projectId);
  } catch(e) {}
}

function renderTasks(projectId) {
  const tasks = state.panels[projectId].taskCache || [];
  const root = document.getElementById(`tasks-${projectId}`);
  const hitlRoot = document.getElementById(`hitl-queue-${projectId}`);
  const banner = document.getElementById(`hitl-banner-${projectId}`);
  if (!root || !hitlRoot) return;
  const hitl = tasks.filter(t => t.status === 'pending-hitl');
  banner.style.display = hitl.length ? 'block' : 'none';
  hitlRoot.innerHTML = hitl.map(t => renderHitlRow(projectId, t)).join('');
  hitl.forEach(t => wireHitlRow(projectId, t));
  root.innerHTML = tasks.map(t => renderTaskRow(t)).join('');
}

function renderTaskRow(t) {
  const claimBadge = t.claimed_by
    ? `<span class="claim-badge" title="claimed at ${escapeHtml(t.claimed_at || '')}">\U0001f512 ${escapeHtml((t.claimed_by_human_id || t.claimed_by_session_name || t.claimed_by || '').slice(0, 16))}</span>`
    : '';
  const deleteBtn = `<button title="Delete from task log (permanent)" onclick="deleteTaskRow(event,'${t.id}','${t.status}')" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:0 4px;flex-shrink:0;line-height:1" onmouseenter="this.style.color='var(--status-failed)'" onmouseleave="this.style.color='var(--muted)'">\u00d7</button>`;
  return `
    <div class="task ${t.status}" id="task-row-${t.id}" style="display:flex;align-items:flex-start;gap:4px">
      <span class="status-badge">${t.status}</span>
      <div style="flex:1;min-width:0">
        <div class="desc">${escapeHtml(t.description)}</div>
        <div class="meta">${escapeHtml(t.created_at)} ${claimBadge}</div>
      </div>
      ${deleteBtn}
    </div>`;
}

async function deleteTaskRow(e, taskId, status) {
  e.stopPropagation();
  const warn = (status === 'pending' || status === 'in_progress')
    ? 'This task is ' + status + '. Deleting it is permanent. Continue?'
    : 'Permanently delete this task from the log?';
  if (!confirm(warn)) return;
  try {
    await api('/tasks/' + taskId, { method: 'DELETE' });
    const row = document.getElementById('task-row-' + taskId);
    if (row) row.remove();
  } catch(e2) { console.error('Delete failed:', e2); }
}

function renderHitlRow(projectId, t) {
  const isExecute = t.description.startsWith('[EXECUTE]');
  const label = isExecute ? 'EXECUTE REQUEST' : 'QUESTION';
  const body = t.description.replace(/^\[(ASK|EXECUTE)\]:?\s*/, '');
  if (isExecute) {
    return `
      <div class="hitl-row" data-task="${t.id}">
        <div class="prompt"><strong>${label}</strong> · ${escapeHtml(body)}</div>
        <div class="controls">
          <button class="execute" data-action="confirm" data-task="${t.id}">EXECUTE</button>
          <button class="danger"  data-action="reject"  data-task="${t.id}">REJECT</button>
        </div>
      </div>`;
  }
  return `
    <div class="hitl-row" data-task="${t.id}">
      <div class="prompt"><strong>${label}</strong> · ${escapeHtml(body)}</div>
      <div class="controls">
        <input type="text" placeholder="your reply" data-input="${t.id}">
        <button class="primary" data-action="reply" data-task="${t.id}">reply</button>
      </div>
    </div>`;
}

function wireHitlRow(projectId, t) {
  const row = document.querySelector(`#hitl-queue-${projectId} [data-task="${t.id}"]`);
  if (!row) return;
  row.querySelectorAll('button[data-action]').forEach(btn => {
    btn.onclick = () => {
      const action = btn.dataset.action;
      if (action === 'reply') {
        const inp = row.querySelector(`input[data-input="${t.id}"]`);
        const text = (inp && inp.value || '').trim();
        if (!text) { toast('enter a reply first', true); return; }
        hitlReply(projectId, t.id, text);
      } else if (action === 'confirm') {
        hitlExecute(projectId, t.id, true);
      } else if (action === 'reject') {
        hitlExecute(projectId, t.id, false);
      }
    };
  });
}

async function appendToGoal(projectId, line) {
  // Pull latest goal, append, push back. String-only for HITL markers.
  let current = '';
  try {
    const goal = await api(`/projects/${projectId}/goal`);
    current = typeof goal.content === 'string' ? goal.content : JSON.stringify(goal.content, null, 2);
  } catch(e) { /* unset goal is fine */ }
  const next = current ? current.trimEnd() + '\n' + line : line;
  await api(`/projects/${projectId}/goal`, { method: 'POST', body: JSON.stringify({ content: next }) });
}

async function hitlReply(projectId, taskId, text) {
  try {
    await appendToGoal(projectId, `[HITL-REPLY:${taskId}:] ${text}`);
    await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done', description: `[ANSWERED] ${text}` }) });
    toast('reply sent');
  } catch(e) { toast('reply failed: ' + e.message, true); }
}

async function hitlExecute(projectId, taskId, confirmed) {
  try {
    if (confirmed) {
      await appendToGoal(projectId, `[EXECUTE-CONFIRMED:${taskId}]`);
      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) });
      toast('execute confirmed');
    } else {
      await appendToGoal(projectId, `[EXECUTE-REJECTED:${taskId}]`);
      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'failed' }) });
      toast('execute rejected');
    }
  } catch(e) { toast('execute failed: ' + e.message, true); }
}

function connectWs(projectId) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/${projectId}`);
  const dot = document.getElementById(`ws-${projectId}`);
  ws.onopen = () => { dot && dot.classList.add('connected'); };
  ws.onclose = () => {
    dot && dot.classList.remove('connected');
    // Reconnect after a beat if the tab is still open.
    setTimeout(() => {
      if (state.panels[projectId]) connectWs(projectId);
    }, 1500);
  };
  ws.onerror = () => { dot && dot.classList.remove('connected'); };
  ws.onmessage = (ev) => {
    try {
      const event = JSON.parse(ev.data);
      handleWsEvent(projectId, event);
    } catch(e){}
  };
  state.panels[projectId].ws = ws;
}

function handleWsEvent(projectId, event) {
  if (event.type === 'update_available') {
    const banner = document.getElementById('update-banner');
    if (banner) banner.style.display = 'block';
    return;
  }
  // v1.9.x — sync tab label and project state on rename from any session.
  if (event.type === 'project_renamed') {
    const tab = state.tabs.find(t => t.id === event.project_id);
    if (tab) {
      tab.project = { ...tab.project, name: event.name };
      const hdr = document.querySelector(`#drawer-status-${event.project_id} .drawer-header span:first-child`);
      if (hdr) hdr.textContent = 'STATUS · ' + event.name;
      renderTabs();
    }
    const proj = state.projects.find(p => p.id === event.project_id);
    if (proj) { proj.name = event.name; loadProjects(); }
    return;
  }
  const cache = state.panels[projectId].taskCache;
  if (event.type === 'task_created') {
    cache.unshift(event.task);
  } else if (event.type === 'task_updated') {
    const i = cache.findIndex(t => t.id === event.task.id);
    if (i >= 0) cache[i] = event.task;
    else cache.unshift(event.task);
  }
  renderTasks(projectId);
  // A goal change is often triggered by a HITL reply — re-pull.
  refreshGoal(projectId);
  // v1.6.x — keep the LIVE tab fresh when it's the visible panel.
  // v1.7.0 — throttle WS bursts via scheduleLiveRefresh (10s floor).
  const panel = state.panels[projectId];
  if (panel && panel.activeVtab === 'live') {
    scheduleLiveRefresh(projectId);
  }
}

async function sendChat(projectId) {
  const input = document.getElementById(`chat-input-${projectId}`);
  const text = input.value.trim();
  if (!text) return;
  const panel = state.panels[projectId];
  const mode = (panel && panel.chatMode) || 'cli';
  // CLI mode uses the claude binary's own auth; only API mode needs
  // ANTHROPIC_API_KEY / OAuth wired into the Anthropic SDK.
  if (mode === 'api' && !state.apiKeyConfigured) {
    toast('No auth configured — set ANTHROPIC_API_KEY or switch to CLI mode', true);
    return;
  }
  input.value = '';
  panel.chatHistory.push({ role: 'user', content: text });
  const history = document.getElementById(`chat-${projectId}`);
  appendChatMessage(history, 'user', text);

  // Build the system prompt from current goal + last 20 tasks so the
  // model has shared context, but don't show it in the UI.
  let systemPrompt = `You are assisting on a Meridian project.`;
  try {
    const goal = await api(`/projects/${projectId}/goal`).catch(() => null);
    if (goal) systemPrompt += `\n\n# Goal (v${goal.version})\n${typeof goal.content === 'string' ? goal.content : JSON.stringify(goal.content, null, 2)}`;
  } catch(e) {}
  try {
    const tasks = await api(`/projects/${projectId}/tasks?limit=20`);
    if (tasks.length) {
      systemPrompt += `\n\n# Recent task log (newest first)\n` + tasks.map(t => `[${t.status}] ${t.description}`).join('\n');
    }
  } catch(e) {}

  const assistantNode = appendChatMessage(history, 'assistant', '');
  try {
    const resp = await fetch('/dashboard/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        project_id: projectId,
        messages: panel.chatHistory,
        system_prompt: systemPrompt,
        mode: mode,
        model: (panel && panel.chatModel) || 'claude-sonnet-4-6',
      }),
    });
    if (!resp.ok || !resp.body) {
      assistantNode.textContent = `error: ${resp.status}`;
      assistantNode.classList.add('error');
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let acc = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = chunk.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') break;
        try {
          const obj = JSON.parse(payload);
          if (obj.error) {
            const is429 = /rate.?limit/i.test(obj.error) && obj.error.includes('429');
            acc += is429
              ? '\n\n⚠️ Rate limited — wait ~60s then retry, or switch to Haiku model'
              : `\n\n⚠️ API error: ${obj.error}`;
          } else if (obj.delta) { acc += obj.delta; }
          assistantNode.textContent = acc;
          history.scrollTop = history.scrollHeight;
        } catch(e){}
      }
    }
    panel.chatHistory.push({ role: 'assistant', content: acc });
  } catch(e) {
    assistantNode.textContent = 'error: ' + e.message;
  }
}

function appendChatMessage(history, role, text) {
  const node = document.createElement('div');
  node.className = 'msg ' + role;
  node.textContent = text;
  history.appendChild(node);
  history.scrollTop = history.scrollHeight;
  return node;
}

document.getElementById('new-project-btn').onclick = async () => {
  const inp = document.getElementById('new-project-name');
  const humanInp = document.getElementById('new-project-human');
  const name = inp.value.trim();
  if (!name) return;
  const body = { name };
  const humanId = (humanInp && humanInp.value || '').trim();
  if (humanId) body.human_id = humanId;
  try {
    const p = await api('/projects', { method: 'POST', body: JSON.stringify(body) });
    inp.value = '';
    if (humanInp) humanInp.value = '';
    await loadProjects();
    openTab(p);
  } catch(e) { toast('create failed: ' + e.message, true); }
};

// Sidebar dropdown — switch active project (opens the tab if not
// already open, otherwise just activates it).
{
  const switcher = document.getElementById('project-switcher');
  if (switcher) {
    switcher.addEventListener('change', (ev) => {
      const id = ev.target.value;
      if (!id) return;
      const p = state.projects.find(x => x.id === id);
      if (p) openTab(p);
    });
  }
}

async function restoreTabs() {
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem(TABS_KEY) || '[]'); } catch(e){}
  for (const id of saved) {
    const p = state.projects.find(x => x.id === id);
    if (p) openTab(p);
  }
  // If no tabs were restored, fall back to the persisted active project
  // or — failing that — the first project in the list.
  if (state.tabs.length === 0 && state.projects.length > 0) {
    let preferred = null;
    try { preferred = localStorage.getItem(ACTIVE_PROJECT_KEY); } catch(e) {}
    const fallback = state.projects.find(p => p.id === preferred) || state.projects[0];
    if (fallback) openTab(fallback);
  }
}

(async function init() {
  await loadServerConfig();
  // v1.9.x — show connection setup modal if no meridian.toml exists
  if (typeof window._showConnSetupIfNeeded === 'function') {
    window._showConnSetupIfNeeded(state.serverConfig);
  }
  await loadConfig();
  await loadProjects();
  // v0.6.6 — EZ first-run wizard: if no projects exist, show the overlay
  if (state.projects.length === 0) {
    document.getElementById('ez-wizard').style.display = 'flex';
    return; // don't restore tabs until wizard completes
  }
  await restoreTabs();
  // v1.5.x — polling removed. WebSocket pushes task/goal updates; sessions
  // refresh on initial page load + explicit user action only (tab switch,
  // worker start, etc). Idle dropdowns no longer hammer /sessions every 1s.

  // v1.7.0 — stop server button
  const stopBtn = document.getElementById('stop-server-btn');
  if (stopBtn) {
    stopBtn.onclick = async () => {
      if (!confirm('Stop the Meridian server? You will need to run `pixi run start` to restart.')) return;
      try {
        await api('/admin/shutdown', { method: 'POST' });
        stopBtn.textContent = 'Stopped — run pixi run start';
        stopBtn.disabled = true;
        // Hide restart button — server is stopped, restart is impossible
        const restartBtn = document.getElementById('restart-server-btn');
        if (restartBtn) restartBtn.style.display = 'none';
      } catch(e) {
        toast('Shutdown request sent.', false);
      }
    };
  }

  // v1.9.x — restart button in sidebar footer
  const restartBtn = document.getElementById('restart-server-btn');
  if (restartBtn) {
    restartBtn.onclick = async () => {
      if (!confirm('Restart the Meridian server?')) return;
      await _doRestart();
    };
  }

  // v1.9.x — restart button in update banner
  const bannerRestartBtn = document.getElementById('banner-restart-btn');
  if (bannerRestartBtn) {
    bannerRestartBtn.onclick = async () => { await _doRestart(); };
  }

  // v1.9.x — git remote warning: poll every 60s, show yellow banner if remote ahead
  async function _checkGitStatus() {
    try {
      const data = await api('/admin/git-status');
      const banner = document.getElementById('git-banner');
      const msg = document.getElementById('git-banner-msg');
      if (banner && data.warning) {
        if (msg) msg.textContent = data.warning;
        banner.style.display = 'block';
      }
    } catch(_) {}
  }
  _checkGitStatus();
  setInterval(_checkGitStatus, 60000);
})();

const _sprintBoardReloaders = {};

async function _deleteSprintItem(projectId, itemId) {
  if (!confirm('Remove this sprint item?')) return;
  try {
    await api(`/projects/${projectId}/sprint-items/${itemId}`, { method: 'DELETE' });
    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();
  } catch(e) { console.error('Delete sprint item failed:', e); }
}

async function _sprintAction(projectId, itemId, action) {
  try {
    await api(`/projects/${projectId}/sprint-items/${itemId}/${action}`, { method: 'POST' });
    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();
  } catch(e) { console.error('Sprint action failed:', action, e); }
}

async function completeSprintItem(projectId, itemId) {
  try {
    await api(`/projects/${projectId}/sprint-items/${itemId}/complete`, { method: 'POST' });
    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();
  } catch(e) { console.error('Complete sprint item failed:', e); }
}

async function failSprintItem(projectId, itemId) {
  try {
    await api(`/projects/${projectId}/sprint-items/${itemId}/fail`, { method: 'POST' });
    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();
  } catch(e) { console.error('Fail sprint item failed:', e); }
}


// --- v0.6.6 EZ wizard ---
// v0.6.6 — EZ wizard logic
document.getElementById('ez-create-btn').onclick = async () => {
  const nameEl = document.getElementById('ez-project-name');
  const humanEl = document.getElementById('ez-human-name');
  const errEl = document.getElementById('ez-error');
  const name = nameEl.value.trim();
  if (!name) { errEl.textContent = 'project name is required'; errEl.style.display = 'block'; return; }
  errEl.style.display = 'none';
  try {
    const body = { name };
    if (humanEl.value.trim()) body.human_id = humanEl.value.trim();
    const p = await api('/projects', { method: 'POST', body: JSON.stringify(body) });
    document.getElementById('ez-wizard').style.display = 'none';
    await loadProjects();
    await restoreTabs();
    openTab(p);
  } catch(e) { errEl.textContent = 'create failed: ' + e.message; errEl.style.display = 'block'; }
};
document.getElementById('ez-project-name').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.getElementById('ez-create-btn').click();
});
document.getElementById('ez-advanced-link').onclick = (e) => {
  e.preventDefault();
  document.getElementById('ez-wizard').style.display = 'none';
  // Show the sidebar new-project form and focus it
  document.getElementById('new-project-name').focus();
  restoreTabs();
};

// v1.9.x — connection setup modal
(function() {
  const modal = document.getElementById('conn-setup-modal');
  const localBtn = document.getElementById('conn-local-btn');
  const sqliteForm = document.getElementById('conn-sqlite-form');
  const sqlitePath = document.getElementById('conn-sqlite-path');
  const sqliteName = document.getElementById('conn-sqlite-name');
  const sqliteSave = document.getElementById('conn-sqlite-save-btn');
  const pgToggle = document.getElementById('conn-pg-toggle-btn');
  const pgForm = document.getElementById('conn-pg-form');
  const pgSave = document.getElementById('conn-pg-save-btn');
  const pgUrl = document.getElementById('conn-pg-url');
  const pgName = document.getElementById('conn-pg-name');
  const errEl = document.getElementById('conn-setup-err');
  if (!modal) return;

  function showErr(msg) { if (errEl) { errEl.textContent = msg; errEl.style.display = msg ? 'block' : 'none'; } }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const m = document.getElementById('conn-setup-modal');
      if (m && m.style.display !== 'none') m.style.display = 'none';
    }
  });

  window._showConnSetupIfNeeded = (cfg) => {
    if (!cfg?.toml_exists && cfg?.db !== 'postgres') modal.style.display = 'flex';
    // Show config file path
    const pathEl = document.getElementById('conn-toml-path');
    if (pathEl && cfg?.toml_path) {
      pathEl.innerHTML = '📄 Config: <span style="color:var(--text)">' + escapeHtml(cfg.toml_path) + '</span>';
    }
  };

  const PG_BTN_STYLE = 'padding:12px;font-size:12px;text-align:left;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);cursor:pointer;font-family:\'IBM Plex Mono\',monospace';
  const PRIMARY_BTN_STYLE = 'padding:12px;font-size:12px;text-align:left;border-radius:4px;cursor:pointer;font-family:\'IBM Plex Mono\',monospace;background:var(--accent);border:1px solid var(--accent);color:#001020;font-weight:600';

  function setActiveBtn(which) {
    // which: 'sqlite' | 'postgres' | null
    if (localBtn) localBtn.style.cssText = which === 'sqlite' ? PRIMARY_BTN_STYLE : PG_BTN_STYLE;
    if (pgToggle) pgToggle.style.cssText = which === 'postgres' ? PRIMARY_BTN_STYLE : PG_BTN_STYLE;
  }

  // Local SQLite toggle — show path form
  if (localBtn) localBtn.onclick = () => {
    if (!sqliteForm) return;
    const open = sqliteForm.style.display === 'flex';
    sqliteForm.style.display = open ? 'none' : 'flex';
    if (!open && sqlitePath && !sqlitePath.value) sqlitePath.value = 'data/meridian.db';
    setActiveBtn(open ? null : 'sqlite');
    // Close postgres form
    if (pgForm) pgForm.style.display = 'none';
  };

  if (sqliteSave) sqliteSave.onclick = async () => {
    const path = sqlitePath?.value.trim() || 'data/meridian.db';
    const name = sqliteName?.value.trim() || 'local';
    showErr('');
    try {
      sqliteSave.textContent = 'Saving…'; sqliteSave.disabled = true;
      await api('/config/connections', {
        method: 'POST',
        body: JSON.stringify({ name, type: 'sqlite', path, activate: true }),
      });
      modal.style.display = 'none';
      toast('Saved — restarting…');
      await _doRestart();
    } catch(e) {
      showErr('Failed: ' + e.message);
      sqliteSave.textContent = 'Save & Restart →'; sqliteSave.disabled = false;
    }
  };

  if (pgToggle) pgToggle.onclick = () => {
    if (pgForm) {
      const open = pgForm.style.display === 'flex';
      pgForm.style.display = open ? 'none' : 'flex';
      setActiveBtn(open ? null : 'postgres');
    }
    // Close sqlite form
    if (sqliteForm) sqliteForm.style.display = 'none';
  };

  if (pgSave) pgSave.onclick = async () => {
    const url = pgUrl?.value.trim() || '';
    const name = pgName?.value.trim() || 'postgres';
    showErr('');
    if (!url) { showErr('Postgres URL is required'); return; }
    try {
      pgSave.textContent = 'Saving…'; pgSave.disabled = true;
      await api('/config/connections', {
        method: 'POST',
        body: JSON.stringify({ name, type: 'postgres', url, activate: true }),
      });
      modal.style.display = 'none';
      toast('Saved — restarting…');
      await _doRestart();
    } catch(e) {
      showErr('Failed: ' + e.message);
      pgSave.textContent = 'Save & Restart →'; pgSave.disabled = false;
    }
  };
})();

// ---------------------------------------------------------------------------
// v1.3.0 — Rewind tab. Renders "Last X days" project recaps. The window
// buttons live in the drawer header (see buildTabBody); the panel here
// just wires their clicks + the "copy shareable link" / load handlers.
// ---------------------------------------------------------------------------

function initRewindTab(projectId) {
  const p = state.panels[projectId];
  if (!p) return;
  if (p.rewindWired) {
    // Already set up — repaint if we already have a window selected.
    if (p.rewindDays) loadRewindTab(projectId, p.rewindDays);
    return;
  }
  p.rewindWired = true;
  document.querySelectorAll(`.rewind-day-btn[data-pid="${projectId}"]`).forEach(btn => {
    btn.onclick = () => {
      const days = parseInt(btn.dataset.days, 10) || 7;
      loadRewindTab(projectId, days);
    };
  });
  const shareBtn = document.getElementById(`rewind-share-${projectId}`);
  if (shareBtn) shareBtn.onclick = () => copyRewindLink(projectId);
  // Default to the 7-day view on first open.
  loadRewindTab(projectId, 7);
}

function toggleExpand(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : '';
  const trigger = el.previousElementSibling;
  if (trigger) {
    const arrow = trigger.querySelector('.expand-arrow');
    if (arrow) arrow.textContent = open ? '▶' : '▼';
  }
}

async function loadRewindTab(projectId, days) {
  const wrap = document.getElementById(`rewind-wrap-${projectId}`);
  if (!wrap) return;
  const p = state.panels[projectId];
  if (p) p.rewindDays = days;
  // Highlight the active window button.
  document.querySelectorAll(`.rewind-day-btn[data-pid="${projectId}"]`).forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.days, 10) === days);
  });
  wrap.innerHTML = '<div style="color:var(--muted)">loading rewind…</div>';
  try {
    const [data, history, stats] = await Promise.all([
      api(`/projects/${projectId}/rewind?days=${days}`),
      api(`/projects/${projectId}/goal-history`).catch(() => []),
      api(`/projects/${projectId}/stats?days=30`).catch(() => null),
    ]);
    const activeTab = (p && p.rewindSubtab) || 'versions';
    wrap.innerHTML = renderRewindSubtabs(projectId, data, history, stats, activeTab);
    wrap.querySelectorAll('.rewind-subtab-btn').forEach(btn => {
      btn.onclick = () => {
        const tab = btn.dataset.tab;
        if (p) p.rewindSubtab = tab;
        wrap.querySelectorAll('.rewind-subtab-btn').forEach(b =>
          b.classList.toggle('active', b.dataset.tab === tab));
        wrap.querySelectorAll('.rewind-subtab-pane').forEach(c =>
          { c.style.display = c.dataset.tab === tab ? '' : 'none'; });
        if (tab === 'charts') initRewindCharts(projectId, stats);
      };
    });
    if (activeTab === 'charts' && stats) initRewindCharts(projectId, stats);
  } catch (e) {
    wrap.innerHTML = `<div style="color:var(--status-failed)">rewind failed: ${escapeHtml(e.message)}</div>`;
  }
}

function renderRewindSubtabs(projectId, data, history, stats, activeTab) {
  /** Render rewind content split into five subtabs: Activity, Milestones, Sprint, Goals, Charts. */
  const tabs = [
    { id: 'versions', label: '📦 Milestones' },
    { id: 'sprint',   label: '⚡ Sprint' },
    { id: 'goals',    label: '🎯 Goal' },
    { id: 'activity', label: '📋 Activity' },
    { id: 'charts',   label: '📊 Charts' },
  ];
  const tabBar = `<div class="rewind-subtab-bar">${
    tabs.map(t => `<button class="rewind-subtab-btn${activeTab === t.id ? ' active' : ''}" data-tab="${t.id}">${t.label}</button>`).join('')
  }</div>`;
  const make = (id, html) =>
    `<div class="rewind-subtab-pane" data-tab="${id}" style="${activeTab === id ? '' : 'display:none'}">${html}</div>`;
  return tabBar +
    make('activity', renderRewindActivity(projectId, data)) +
    make('versions', renderRewindVersions(projectId, data)) +
    make('sprint',   renderRewindSprint(projectId, data)) +
    make('goals',    renderRewindGoals(projectId, data, history)) +
    make('charts',   renderRewindCharts(projectId, stats));
}

function renderRewindCharts(projectId, stats) {
  /** Charts subtab: tasks/day bar chart + sprint completion % by version. */
  if (!stats) {
    return '<div style="padding:14px;color:var(--muted);font-size:11px">Charts unavailable — stats endpoint not reachable.</div>';
  }
  return `<div style="padding:8px 0">
    <div style="color:var(--accent);font-weight:600;font-size:11px;margin-bottom:8px">📊 Tasks completed / day (last ${stats.period_days}d)</div>
    <canvas id="chart-tasks-${escapeHtml(projectId)}" style="max-width:100%;max-height:160px"></canvas>
    <div style="color:var(--accent);font-weight:600;font-size:11px;margin:18px 0 8px">⚡ Sprint completion % by version</div>
    <canvas id="chart-sprint-${escapeHtml(projectId)}" style="max-width:100%;max-height:120px"></canvas>
  </div>`;
}

function initRewindCharts(projectId, stats) {
  /** Draw (or redraw) Chart.js instances for the Charts subtab. Destroys prior instances first. */
  if (!stats || typeof Chart === 'undefined') return;
  const p = state.panels[projectId];

  // Destroy stale instances before re-creating to avoid duplicate chart warning.
  if (p) {
    if (p._chartTasks) { p._chartTasks.destroy(); p._chartTasks = null; }
    if (p._chartSprint) { p._chartSprint.destroy(); p._chartSprint = null; }
  }

  const tasksCanvas = document.getElementById(`chart-tasks-${projectId}`);
  if (tasksCanvas && stats.tasks_per_day) {
    const labels = stats.tasks_per_day.map(d => d.day.slice(5));  // MM-DD
    const totals = stats.tasks_per_day.map(d => d.total);
    const chart = new Chart(tasksCanvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'tasks done',
          data: totals,
          backgroundColor: 'rgba(96, 165, 250, 0.7)',
          borderRadius: 2,
        }],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#9ca3af', font: { size: 9 }, maxRotation: 45 }, grid: { color: '#1f2937' } },
          y: { ticks: { color: '#9ca3af', font: { size: 9 }, stepSize: 1 }, grid: { color: '#1f2937' }, beginAtZero: true },
        },
      },
    });
    if (p) p._chartTasks = chart;
  }

  const sprintCanvas = document.getElementById(`chart-sprint-${projectId}`);
  if (sprintCanvas && stats.sprint_velocity && stats.sprint_velocity.length) {
    const sv = stats.sprint_velocity;
    const chart = new Chart(sprintCanvas, {
      type: 'bar',
      data: {
        labels: sv.map(v => v.version),
        datasets: [{
          label: '% done',
          data: sv.map(v => v.pct),
          backgroundColor: sv.map(v => v.pct === 100 ? 'rgba(52, 211, 153, 0.7)' : 'rgba(96, 165, 250, 0.7)'),
          borderRadius: 2,
        }],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#9ca3af', font: { size: 9 } }, grid: { color: '#1f2937' } },
          y: { ticks: { color: '#9ca3af', font: { size: 9 } }, grid: { color: '#1f2937' }, min: 0, max: 100,
               title: { display: true, text: '% done', color: '#9ca3af', font: { size: 9 } } },
        },
      },
    });
    if (p) p._chartSprint = chart;
  }
}

function renderRewindSprint(projectId, data) {
  /** Sprint subtab: sprint_items grouped by version, showing done/pending/failed counts. */
  const items = data.sprint_items_completed || [];
  const pending = data.sprint_items_pending || [];
  const allItems = [...items, ...pending];

  if (!allItems.length) {
    return '<div style="padding:14px;color:var(--muted);font-size:11px">No sprint items yet.</div>';
  }

  // Group by version
  const byVersion = {};
  allItems.forEach(s => {
    const v = s.version || 'current';
    if (!byVersion[v]) byVersion[v] = [];
    byVersion[v].push(s);
  });

  const statusDot = (s) => {
    if (s.status === 'done') return '<span style="color:var(--status-done)">✓</span>';
    if (s.status === 'failed') return '<span style="color:var(--status-failed)">✗</span>';
    if (s.status === 'pushed') return '<span style="color:var(--muted)">→</span>';
    return '<span style="color:var(--status-pending)">○</span>';
  };

  let html = '';
  // Sort versions: current first, then descending
  const versions = Object.keys(byVersion).sort((a, b) => {
    if (a === 'current') return -1;
    if (b === 'current') return 1;
    return b.localeCompare(a);
  });

  versions.forEach(v => {
    const vItems = byVersion[v];
    const doneCount = vItems.filter(s => s.status === 'done').length;
    const total = vItems.length;
    const pct = total ? Math.round((doneCount / total) * 100) : 0;
    const id = `sprint-v-${projectId}-${v.replace(/[^a-z0-9]/gi, '')}`;
    html += `<section style="margin-bottom:10px">
      <div style="cursor:pointer;display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border)" onclick="toggleExpand('${id}')">
        <span style="color:var(--accent);font-weight:600;font-size:11px">${escapeHtml(v)}</span>
        <span style="color:var(--muted);font-size:10px">${doneCount}/${total} done (${pct}%) <span class="expand-arrow">▶</span></span>
      </div>
      <div id="${id}" style="display:${v === 'current' ? 'block' : 'none'}">
        ${vItems.map(s => `<div style="padding:3px 0 3px 8px;font-size:11px;display:flex;gap:6px;align-items:flex-start">
          ${statusDot(s)}
          <span style="color:${s.status === 'done' ? 'var(--muted)' : 'var(--text)'};${s.status === 'done' ? 'text-decoration:line-through' : ''}">${escapeHtml(s.title || '')}</span>
        </div>`).join('')}
      </div>
    </section>`;
  });

  return `<div style="padding:8px 0">${html}</div>`;
}

function _rewindSec(icon, title, items, render) {
  /** Shared section renderer for rewind subtabs. */
  if (!items || !items.length) {
    return `<section style="margin-bottom:14px">
      <div style="color:var(--accent);font-weight:600;margin-bottom:4px">${icon} ${title}</div>
      <div style="color:var(--muted);font-size:10px">(none)</div>
    </section>`;
  }
  return `<section style="margin-bottom:14px">
    <div style="color:var(--accent);font-weight:600;margin-bottom:4px">${icon} ${title}</div>
    ${items.map(render).join('')}
  </section>`;
}

function renderRewindActivity(projectId, data) {
  /** Activity subtab: sessions + decisions + task stats. */
  const sessions = _rewindSec('🧠', 'Sessions', data.session_summaries, s =>
    `<div style="padding:3px 0;border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">
      <div style="color:var(--accent)">${escapeHtml(s.session_name)} <span style="color:var(--muted);font-size:10px">· ${s.tasks_completed} done</span></div>
      <div style="color:var(--muted);font-size:10px">${escapeHtml(s.summary || '')}</div>
    </div>`);
  const decisions = _rewindSec('📋', 'Decisions logged', data.decisions_logged, d =>
    `<div style="padding:2px 0"><span style="color:var(--muted);font-size:10px">[${escapeHtml(d.logged_at || '')}]</span> ${escapeHtml(d.text || '')}</div>`);
  const byStatus = data.tasks_by_status || {};
  const summary = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">
    <div style="color:var(--accent);font-weight:600">📊 Tasks: ${byStatus.done || 0} done, ${byStatus.failed || 0} failed, ${byStatus.pending || 0} pending <span style="color:var(--muted);font-size:10px">(${data.tasks_total || 0} total over ${data.period_days}d)</span></div>
  </section>`;
  return sessions + decisions + summary;
}

function renderRewindVersions(projectId, data) {
  /** Versions subtab: milestones shipped + sprint items completed + stats. */
  const versions = _rewindSec('📦', 'Milestones shipped', data.versions_shipped,
    v => `<div style="padding:5px 0;border-bottom:1px solid var(--border);font-size:11px;white-space:pre-wrap;word-break:break-word;line-height:1.6;color:var(--text)">${escapeHtml(v)}</div>`);
  const sprints = _rewindSec('✅', 'Sprint items completed', data.sprint_items_completed, s =>
    `<div style="padding:2px 0"><span style="color:var(--accent-green)">${escapeHtml(s.version || '')}</span> — ${escapeHtml(s.title || '')} <span style="color:var(--muted);font-size:10px">${escapeHtml(s.completed_at || '')}</span></div>`);
  const byStatus = data.tasks_by_status || {};
  const summary = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">
    <div style="color:var(--accent);font-weight:600">📊 ${byStatus.done || 0} tasks completed over ${data.period_days}d</div>
  </section>`;
  return versions + sprints + summary;
}

function renderRewindGoals(projectId, data, history) {
  /** Goals subtab: goal changes (newest first) + goal version history. */
  const preStyle = 'margin:0;white-space:pre-wrap;word-break:break-word;background:var(--bg-card);padding:6px;border-radius:3px;font-size:10px;font-family:inherit';
  const goals = _rewindSec('🎯', 'Goal changes', (data.goal_changes || []).slice().reverse(), (g, idx) => {
    const id = `gc-expand-${projectId}-${idx}`;
    return `<div style="padding:3px 0;border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">
      <div style="cursor:pointer;user-select:none" onclick="toggleExpand('${id}')">
        <div style="color:var(--muted);font-size:10px">${escapeHtml(g.field)} · ${escapeHtml(g.changed_at || '')} <span class="expand-arrow" style="font-size:9px">▶</span></div>
        <div style="color:var(--text);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((g.new_summary || '(empty)').slice(0,120))}</div>
      </div>
      <div id="${id}" style="display:none;margin-top:6px;overflow:visible;max-height:none">
        <div style="color:var(--muted);font-size:10px;margin-bottom:2px">before:</div>
        <pre style="${preStyle};margin-bottom:6px">${escapeHtml(g.old_full || '(empty)')}</pre>
        <div style="color:var(--muted);font-size:10px;margin-bottom:2px">after:</div>
        <pre style="${preStyle}">${escapeHtml(g.new_full || '(empty)')}</pre>
      </div>
    </div>`;
  });
  let historyHtml = '';
  if (history && history.length) {
    const rows = [...history].reverse().map((v, idx) => {
      const id = `gv-expand-${projectId}-${idx}`;
      const raw = (v.version_goal || v.north_star || '').replace(/\s+/g, ' ').trim();
      const snippet = raw.length > 80 ? raw.slice(0, 79) + '…' : raw;
      return `<div style="border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">
        <div style="cursor:pointer;user-select:none" onclick="toggleExpand('${id}')">
          <span style="color:var(--accent)">v${v.version}</span>
          <span style="color:var(--muted);font-size:10px"> · ${escapeHtml(v.created_at || '')}</span>
          <span> ${escapeHtml(snippet)}</span>
          <span class="expand-arrow" style="color:var(--muted);font-size:9px"> ▶</span>
        </div>
        <div id="${id}" style="display:none;margin-top:6px">
          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">north_star:</div>
          <pre style="${preStyle};margin-bottom:6px">${escapeHtml(v.north_star || '(empty)')}</pre>
          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">version_goal:</div>
          <pre style="${preStyle};margin-bottom:6px">${escapeHtml(v.version_goal || '(empty)')}</pre>
          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">sprint:</div>
          <pre style="${preStyle}">${escapeHtml(v.sprint || '(empty)')}</pre>
        </div>
      </div>`;
    });
    historyHtml = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">
      <div style="color:var(--accent);font-weight:600;margin-bottom:6px">📜 Goal version history (${history.length} versions, newest first)</div>
      ${rows.join('')}
    </section>`;
  }
  return goals + historyHtml;
}

async function copyRewindLink(projectId) {
  const p = state.panels[projectId];
  const days = (p && p.rewindDays) || 7;
  try {
    const res = await api(`/projects/${projectId}/rewind-token`, { method: 'POST' });
    let base = '';
    try {
      const cfg = await api('/config');
      base = cfg.server_url || window.location.origin;
    } catch (_) {
      base = window.location.origin;
    }
    const url = `${base}/projects/${projectId}/rewind?days=${days}&token=${encodeURIComponent(res.token)}`;
    try {
      await navigator.clipboard.writeText(url);
      toast('shareable link copied');
    } catch (_) {
      // Older browsers — surface the URL so the user can copy manually.
      window.prompt('copy this URL', url);
    }
  } catch (e) {
    toast('share failed: ' + e.message);
  }
}

