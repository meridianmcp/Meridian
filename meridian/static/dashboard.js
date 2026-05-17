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
  } catch (e) { /* offline / older server — ignore */ }
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
    div.innerHTML = `<span>${escapeHtml(p.name)}</span><span class="id">${p.id.slice(0,6)}</span>`;
    div.onclick = () => openTab(p);
    list.appendChild(div);
  });
  // Mirror the same list into the top-of-sidebar dropdown so users can
  // switch the active project from one place. Selection drives the
  // single-active concept; the multi-tab UI still works on top.
  const switcher = document.getElementById('project-switcher');
  if (switcher) {
    const previous = switcher.value;
    switcher.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = state.projects.length
      ? '— switch project —'
      : '(no projects yet)';
    switcher.appendChild(placeholder);
    state.projects.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      switcher.appendChild(opt);
    });
    if (previous && state.projects.some(p => p.id === previous)) {
      switcher.value = previous;
    }
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

function renderTabs() {
  const bar = document.getElementById('tabs');
  bar.innerHTML = '';
  state.tabs.forEach(t => {
    const div = document.createElement('div');
    div.className = 'tab' + (state.activeTab === t.id ? ' active' : '');
    div.innerHTML = `<span>${escapeHtml(t.project.name)}</span>`;
    div.onclick = () => activateTab(t.id);
    const close = document.createElement('button');
    close.className = 'close';
    close.textContent = '×';
    close.onclick = (e) => { e.stopPropagation(); closeTab(t.id); };
    div.appendChild(close);
    bar.appendChild(div);
  });
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
      <div class="drawer-panel" id="drawer-goal-${project.id}">
        <div class="drawer-header">GOAL · ${escapeHtml(project.name)}</div>
        <div style="flex:1;display:flex;flex-direction:column;padding:12px 14px;gap:10px;overflow-y:auto">
          <div style="display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
            <span class="goal-version" id="goal-version-${project.id}"></span>
            <span style="display:flex;gap:6px;align-items:center">
              <button class="secondary" id="goal-mode-${project.id}" title="Toggle between manual and auto goal mode">mode: manual</button>
            </span>
          </div>
          <div class="goal-section">
            <div class="goal-label">🔭 North Star <span id="goal-ns-lock-${project.id}" style="opacity:0.5"></span><span class="goal-ts" id="goal-ns-ts-${project.id}"></span></div>
            <textarea class="goal-area mono" id="goal-north-star-${project.id}" placeholder="(north star not set)"></textarea>
            <div class="goal-actions"><button class="primary" id="save-north-star-${project.id}">save north star</button></div>
          </div>
          <div class="goal-section">
            <div class="goal-label">◎ Version Goal<span class="goal-ts" id="goal-vg-ts-${project.id}"></span></div>
            <textarea class="goal-area mono" id="goal-${project.id}" placeholder="(no version goal set)" style="flex:1;max-height:none;resize:vertical"></textarea>
            <div class="goal-actions">
              <button class="primary" id="save-goal-${project.id}">save version goal</button>
              <span class="goal-version" id="goal-state-${project.id}"></span>
            </div>
          </div>
          <div class="goal-section">
            <div class="goal-label">⚡ Sprint<span class="goal-ts" id="goal-sp-ts-${project.id}"></span></div>
            <textarea class="goal-area sprint mono" id="goal-sprint-${project.id}" placeholder="(sprint not set)"></textarea>
            <div class="goal-actions"><button class="secondary" id="save-sprint-${project.id}">save sprint</button></div>
          </div>
          <div style="flex-shrink:0;padding:4px 0 8px 0">
            <a class="secondary" style="display:inline-block;padding:5px 12px;border:1px solid var(--border);border-radius:4px;color:var(--muted);font-size:10px;text-decoration:none;font-family:'IBM Plex Mono',monospace;cursor:pointer" href="/projects/${project.id}/export/pdf" download>⬇ Export IP Record (PDF)</a>
          </div>
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
          <textarea id="file-content-${project.id}" style="flex:1;background:var(--surface-2);border:none;border-top:1px solid var(--border);color:var(--text);padding:10px 14px;font-family:'IBM Plex Mono',monospace;font-size:12px;resize:none;outline:none;overflow-y:auto"></textarea>
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
      <div class="claude-cta">
        <p class="claude-cta-title">Talk to Claude in the real chat surface</p>
        <p class="claude-cta-body">
          The dashboard is no longer the conversation. Meridian is the
          source of truth — generate a handoff, paste it into claude.ai,
          and let workers report back via MCP. Faster, cheaper, no API
          credits.
        </p>
        <a class="claude-cta-button" id="open-in-claude-${project.id}"
           href="https://claude.ai/new" target="_blank" rel="noopener">
          Open in Claude →
        </a>
        <a class="claude-cta-secondary" id="copy-handoff-${project.id}"
           href="#">Copy latest handoff to clipboard</a>
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
      };
    });
  }

  document.getElementById(`save-goal-${project.id}`).onclick = () => saveGoal(project.id);
  document.getElementById(`save-north-star-${project.id}`).onclick = () => saveNorthStar(project.id);
  document.getElementById(`save-sprint-${project.id}`).onclick = () => saveSprint(project.id);

  // v0.4.2 — goal-mode toggle. The button label tracks the current
  // mode; clicking it PATCHes the server to flip between manual/auto
  // and toasts the new value.
  const modeBtn = document.getElementById(`goal-mode-${project.id}`);
  if (modeBtn) {
    const renderMode = (m) => { modeBtn.textContent = 'mode: ' + m; };
    api(`/projects/${project.id}/goal-mode`)
      .then(r => renderMode(r.goal_mode || 'manual'))
      .catch(() => {});
    modeBtn.onclick = async () => {
      const current = modeBtn.textContent.replace(/^mode:\s*/, '').trim();
      const next = current === 'auto' ? 'manual' : 'auto';
      try {
        await api(`/projects/${project.id}/goal-mode`, {
          method: 'PATCH', body: JSON.stringify({ mode: next })
        });
        renderMode(next);
        toast('goal mode: ' + next);
      } catch(e) { toast('mode change failed: ' + e.message, true); }
    };
  }

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
  // v1.1.0 — Open in Claude CTA replaces the chat panel.
  const openInClaude = document.getElementById(`open-in-claude-${project.id}`);
  if (openInClaude) openInClaude.href = 'https://claude.ai/new';
  const copyHandoff = document.getElementById(`copy-handoff-${project.id}`);
  if (copyHandoff) copyHandoff.onclick = async (ev) => {
    ev.preventDefault();
    try {
      const r = await fetch(`/projects/${project.id}/handoff`, { method: 'POST' });
      if (!r.ok) throw new Error(`${r.status}`);
      const body = await r.json();
      const text = body.content || '';
      if (text && navigator.clipboard) {
        await navigator.clipboard.writeText(text);
        toast('handoff copied — paste into Claude');
      } else {
        toast('handoff written: ' + (body.path || 'data/'), false);
      }
    } catch(e) { toast('handoff failed: ' + e.message, true); }
  };
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

  // v1.1.0 — marked.js edit/preview toggle per goal field.
  wireGoalPreviewToggle(project.id);

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
  goal_events.forEach(g => {
    events.push({
      ts: g.updated_at || '',
      actor: 'goal',
      desc: `📋 ${g.field} updated → v${g.version}`,
    });
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
  const failed = tasks.filter(t => t.status === 'failed').slice(0, 5);

  const sect = (icon, title, items, emptyMsg) => {
    const rows = items.length
      ? items.map(t => {
          const sessLine = t.session_name
            ? `<div class="queue-item-session">${escapeHtml(t.session_name)}</div>` : '';
          return `<div class="queue-item">${escapeHtml((t.description || '').slice(0, 120))}${sessLine}</div>`;
        }).join('')
      : `<div class="queue-empty">${emptyMsg}</div>`;
    return `<div class="queue-section">` +
      `<div class="queue-section-header">${icon} ${title} <span style="color:var(--accent)">(${items.length})</span></div>` +
      rows + `</div>`;
  };

  return sect('⏳', 'Pending', pending, 'no pending tasks') +
         sect('🔄', 'In Progress', inProg, 'nothing running') +
         sect('✅', 'Recently Done', done, 'no completed tasks') +
         (failed.length ? sect('❌', 'Failed', failed, '') : '');
}

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
    ta.value = text;
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
  } catch (e) {
    ta.value = '';
    v.textContent = '(unset)';
  }
}

// v1.1.0 — marked.js edit/preview toggle for the three goal fields.
// Each textarea gets a small Edit / Preview chip pair injected just
// above it. Preview mode swaps the textarea out for a rendered div
// whose innerHTML is marked(textarea.value). Edit mode swaps back.
function wireGoalPreviewToggle(projectId) {
  const fields = [
    { ta: `goal-${projectId}`,            preview: `preview-goal-${projectId}` },
    { ta: `goal-north-star-${projectId}`, preview: `preview-north-star-${projectId}` },
    { ta: `goal-sprint-${projectId}`,     preview: `preview-sprint-${projectId}` },
  ];
  fields.forEach(({ ta, preview }) => {
    const taEl = document.getElementById(ta);
    if (!taEl) return;
    if (document.getElementById(`row-${ta}`)) return; // already wired
    const row = document.createElement('div');
    row.id = `row-${ta}`;
    row.className = 'preview-toggle-row';
    row.innerHTML =
      `<button class="preview-btn active" data-mode="edit">edit</button>` +
      `<button class="preview-btn"        data-mode="preview">preview</button>`;
    taEl.parentNode.insertBefore(row, taEl);
    const previewDiv = document.createElement('div');
    previewDiv.id = preview;
    previewDiv.className = 'goal-preview';
    previewDiv.style.display = 'none';
    taEl.parentNode.insertBefore(previewDiv, taEl.nextSibling);
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
          previewDiv.innerHTML = html;
          taEl.style.display = 'none';
          previewDiv.style.display = '';
        } else {
          previewDiv.style.display = 'none';
          taEl.style.display = '';
        }
      };
    });
  });
}

async function saveGoal(projectId) {
  const ta = document.getElementById(`goal-${projectId}`);
  if (!ta) return;
  const raw = ta.value;
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
    root.innerHTML = sessions.map(s =>
      `<div class="session-row"><span class="name">${escapeHtml(s.human_id ? s.human_id + '/' + s.name : s.name)}</span><span class="meta">${escapeHtml(s.status)} · ${escapeHtml(formatRelativeTime(s.last_seen))}</span></div>`
    ).join('') || '<div class="session-row meta">(no active sessions)</div>';
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
    ? `<span class="claim-badge" title="claimed at ${escapeHtml(t.claimed_at || '')}">🔒 ${escapeHtml(t.claimed_by.slice(0, 8))}</span>`
    : '';
  return `
    <div class="task ${t.status}">
      <span class="status-badge">${t.status}</span>
      <div>
        <div class="desc">${escapeHtml(t.description)}</div>
        <div class="meta">${escapeHtml(t.created_at)} ${claimBadge}</div>
      </div>
    </div>`;
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
  await loadConfig();
  await loadProjects();
  // v0.6.6 — EZ first-run wizard: if no projects exist, show the overlay
  if (state.projects.length === 0) {
    document.getElementById('ez-wizard').style.display = 'flex';
    return; // don't restore tabs until wizard completes
  }
  await restoreTabs();
  // Periodic session refresh on the active tab — sessions don't generate
  // pub/sub events so polling fills that gap.
  setInterval(() => {
    if (state.activeTab) refreshSessions(state.activeTab);
  }, 10000);
})();

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
    setInterval(() => { if (state.activeTab) refreshSessions(state.activeTab); }, 10000);
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
  restoreTabs().then(() => {
    setInterval(() => { if (state.activeTab) refreshSessions(state.activeTab); }, 10000);
  });
};

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
    const data = await api(`/projects/${projectId}/rewind?days=${days}`);
    wrap.innerHTML = renderRewind(data);
  } catch (e) {
    wrap.innerHTML = `<div style="color:var(--status-failed)">rewind failed: ${escapeHtml(e.message)}</div>`;
  }
}

function renderRewind(data) {
  const sec = (icon, title, items, render) => {
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
  };
  const versions = sec('📦', 'Versions shipped', data.versions_shipped,
    v => `<div style="padding:2px 0">${escapeHtml(v)}</div>`);
  const goals = sec('🎯', 'Goal changes', data.goal_changes, g =>
    `<div style="padding:3px 0;border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">
      <div style="color:var(--muted);font-size:10px">${escapeHtml(g.field)} · ${escapeHtml(g.changed_at || '')}</div>
      <div>${escapeHtml(g.old_summary || '(empty)')} → ${escapeHtml(g.new_summary || '(empty)')}</div>
    </div>`);
  const decisions = sec('📋', 'Decisions logged', data.decisions_logged, d =>
    `<div style="padding:2px 0"><span style="color:var(--muted);font-size:10px">[${escapeHtml(d.logged_at || '')}]</span> ${escapeHtml(d.text || '')}</div>`);
  const sprints = sec('✅', 'Sprint items completed', data.sprint_items_completed, s =>
    `<div style="padding:2px 0"><span style="color:var(--accent-green)">${escapeHtml(s.version || '')}</span> — ${escapeHtml(s.title || '')} <span style="color:var(--muted);font-size:10px">${escapeHtml(s.completed_at || '')}</span></div>`);
  const sessions = sec('🧠', 'Sessions', data.session_summaries, s =>
    `<div style="padding:3px 0;border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">
      <div style="color:var(--accent)">${escapeHtml(s.session_name)} <span style="color:var(--muted);font-size:10px">· ${s.tasks_completed} done</span></div>
      <div style="color:var(--muted);font-size:10px">${escapeHtml(s.summary || '')}</div>
    </div>`);
  const byStatus = data.tasks_by_status || {};
  const summary = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">
    <div style="color:var(--accent);font-weight:600">📊 Tasks: ${byStatus.done || 0} done, ${byStatus.failed || 0} failed, ${byStatus.pending || 0} pending <span style="color:var(--muted);font-size:10px">(${data.tasks_total || 0} total over ${data.period_days}d)</span></div>
  </section>`;
  return versions + goals + decisions + sprints + sessions + summary;
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
