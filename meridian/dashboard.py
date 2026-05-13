"""Dashboard surface for Meridian v0.2.0.

Three pieces:

* :class:`WebSocketBroadcaster` — bridges the in-process pub/sub in
  :mod:`meridian.db` to live WebSocket clients. One instance lives on
  ``app.state.ws_broadcaster``.
* :func:`stream_anthropic_chat` — async generator that proxies the
  Anthropic streaming API and yields Server-Sent-Event lines.
* :data:`DASHBOARD_HTML` — the entire single-file dashboard, served by
  ``GET /dashboard``. No build step, no external assets except the IBM
  Plex Mono font loaded from Google Fonts.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator

from fastapi import WebSocket

from . import db as db_module


# ---------------------------------------------------------------------------
# WebSocket broadcaster
# ---------------------------------------------------------------------------


class WebSocketBroadcaster:
    """Forward task-log pub/sub events to subscribed WebSocket clients.

    One :class:`asyncio.Queue` is registered per WebSocket via
    :func:`meridian.db.subscribe_tasks`. A pump task drains the queue and
    writes JSON frames to the socket. The broadcaster is created once at
    app startup and torn down on shutdown.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def serve(self, ws: WebSocket, project_id: str) -> None:
        """Accept a WebSocket and pipe task events to it until it closes.

        Reads from the socket are drained on a separate task so we notice
        when the client disconnects. We don't act on inbound messages
        (the dashboard pushes via HTTP, not WS) but we have to read them
        for the close handshake to land.
        """
        await ws.accept()
        queue = db_module.subscribe_tasks(project_id)

        async def reader() -> None:
            try:
                while True:
                    await ws.receive_text()
            except Exception:
                pass

        reader_task = asyncio.create_task(reader())
        try:
            while True:
                event = await queue.get()
                try:
                    await ws.send_text(json.dumps(event, default=str))
                except Exception:
                    break
        finally:
            reader_task.cancel()
            db_module.unsubscribe_tasks(project_id, queue)
            try:
                await ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Anthropic chat proxy (Server-Sent Events)
# ---------------------------------------------------------------------------


async def stream_anthropic_chat(
    messages: list[dict[str, str]],
    system_prompt: str | None,
    model: str,
    max_tokens: int,
) -> AsyncIterator[bytes]:
    """Yield Server-Sent-Event lines for an Anthropic streaming response.

    Reads ``ANTHROPIC_API_KEY`` from the environment at call time so the
    key never reaches the browser. Each text delta is emitted as a
    ``data: {...}`` line; a terminating ``data: [DONE]`` line is sent on
    success or a single ``data: {"error": "..."}`` line on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        payload = json.dumps(
            {"error": "ANTHROPIC_API_KEY not set on server"}
        )
        yield f"data: {payload}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    # Import the SDK lazily so test runs that mock this function don't
    # pay the import cost.
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        payload = json.dumps(
            {"error": f"anthropic SDK not installed: {exc}"}
        )
        yield f"data: {payload}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    client = AsyncAnthropic(api_key=api_key)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system_prompt:
        kwargs["system"] = system_prompt

    try:
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                payload = json.dumps({"delta": text})
                yield f"data: {payload}\n\n".encode("utf-8")
    except Exception as exc:  # noqa: BLE001 — surface to browser
        payload = json.dumps(
            {"error": f"{type(exc).__name__}: {exc}"}
        )
        yield f"data: {payload}\n\n".encode("utf-8")

    yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Meridian Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0d0f12;
  --surface: #13161b;
  --surface-2: #1a1e25;
  --border: #232830;
  --text: #d8dde6;
  --muted: #7a8190;
  --accent: #4a9eff;
  --accent-green: #00d4aa;
  --status-pending: #f0c674;
  --status-done: #00d4aa;
  --status-failed: #ff6b6b;
  --status-hitl: #ff9f43;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; height: 100%; overflow: hidden;
  background: var(--bg); color: var(--text);
  font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 13px;
}
.mono { font-family: 'IBM Plex Mono', ui-monospace, monospace; }
.app { display: grid; grid-template-columns: 220px 1fr; height: 100vh; }

.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
}
.sidebar-header {
  padding: 14px 16px; border-bottom: 1px solid var(--border);
  font-family: 'IBM Plex Mono', monospace; font-weight: 600;
  letter-spacing: 0.04em; color: var(--accent);
}
.sidebar-header small { display: block; color: var(--muted); font-weight: 400; font-size: 11px; }
#api-warn {
  margin: 10px; padding: 8px 10px;
  background: rgba(255,107,107,0.1); border: 1px solid rgba(255,107,107,0.3);
  border-radius: 4px; color: var(--status-failed); font-size: 11px;
  display: none;
}
.projects-label {
  padding: 10px 16px 4px; color: var(--muted); font-size: 11px;
  letter-spacing: 0.06em; text-transform: uppercase;
  font-family: 'IBM Plex Mono', monospace;
}
.project-list { flex: 1; overflow-y: auto; padding: 4px 8px 16px; }
.project-item {
  padding: 8px 10px; border-radius: 4px; cursor: pointer;
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  display: flex; justify-content: space-between; align-items: center;
}
.project-item:hover { background: var(--surface-2); }
.project-item .id { color: var(--muted); font-size: 10px; margin-left: 8px; }

.new-project {
  padding: 8px 16px; border-top: 1px solid var(--border);
  display: flex; gap: 6px;
}
.new-project input { flex: 1; }

.main { display: flex; flex-direction: column; min-width: 0; }
.tabs {
  background: var(--surface); border-bottom: 1px solid var(--border);
  display: flex; align-items: stretch; overflow-x: auto; min-height: 40px;
}
.tab {
  padding: 10px 14px; border-right: 1px solid var(--border);
  cursor: pointer; display: flex; gap: 8px; align-items: center;
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  background: var(--surface); color: var(--muted);
  white-space: nowrap;
}
.tab.active { background: var(--bg); color: var(--text); border-bottom: 2px solid var(--accent); }
.tab .close {
  color: var(--muted); border: none; background: transparent;
  cursor: pointer; padding: 0 2px; font-size: 14px;
}
.tab .close:hover { color: var(--status-failed); }
.tab-bodies { flex: 1; overflow: hidden; position: relative; }
.tab-body {
  position: absolute; inset: 0;
  display: none; grid-template-columns: 1fr 1fr;
}
.tab-body.active { display: grid; }

.empty {
  display: flex; align-items: center; justify-content: center;
  height: 100%; color: var(--muted);
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
}

.panel { display: flex; flex-direction: column; overflow: hidden; min-width: 0; }
.panel.left { border-right: 1px solid var(--border); background: var(--bg); }
.panel.right { background: var(--surface); }
.panel-header {
  padding: 10px 14px; border-bottom: 1px solid var(--border);
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase;
  display: flex; justify-content: space-between; align-items: center;
}
.panel-header .ws-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--status-failed); display: inline-block;
}
.panel-header .ws-dot.connected { background: var(--status-done); }

.section {
  padding: 12px 14px; border-bottom: 1px solid var(--border);
}
.section h3 {
  margin: 0 0 8px; font-size: 11px; color: var(--muted);
  letter-spacing: 0.06em; text-transform: uppercase;
  font-family: 'IBM Plex Mono', monospace; font-weight: 600;
}
.goal-area {
  width: 100%; min-height: 90px; max-height: 280px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 4px; padding: 8px; color: var(--text);
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  resize: vertical;
}
.goal-actions { display: flex; gap: 6px; margin-top: 6px; align-items: center; }
.goal-version { color: var(--muted); font-size: 11px; font-family: 'IBM Plex Mono', monospace; }

button.primary, button.secondary, button.danger {
  border: 1px solid var(--border); background: var(--surface-2);
  color: var(--text); padding: 6px 12px; border-radius: 4px;
  cursor: pointer; font-family: 'IBM Plex Mono', monospace; font-size: 11px;
}
button.primary { background: var(--accent); border-color: var(--accent); color: #001020; font-weight: 600; }
button.primary:hover { filter: brightness(1.1); }
button.secondary:hover { background: var(--border); }
button.danger { background: rgba(255,107,107,0.15); border-color: var(--status-failed); color: var(--status-failed); }
button.danger:hover { background: rgba(255,107,107,0.25); }
button.execute { background: var(--status-hitl); border-color: var(--status-hitl); color: #1a0f00; font-weight: 700; }
button.execute:hover { filter: brightness(1.1); }

input[type=text] {
  background: var(--surface-2); border: 1px solid var(--border);
  color: var(--text); padding: 6px 8px; border-radius: 4px;
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
}

.sessions-list { display: flex; flex-direction: column; gap: 4px; }
.session-row {
  padding: 4px 8px; background: var(--surface-2); border-radius: 4px;
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  display: flex; justify-content: space-between;
}
.session-row .name { color: var(--text); }
.session-row .meta { color: var(--muted); }

.scroll-area { flex: 1; overflow-y: auto; }

.task-list { display: flex; flex-direction: column; }
.task {
  padding: 8px 14px; border-bottom: 1px solid var(--border);
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  display: flex; gap: 10px; align-items: flex-start;
}
.task .status-badge {
  font-size: 10px; padding: 2px 6px; border-radius: 3px;
  text-transform: uppercase; letter-spacing: 0.04em;
  font-weight: 600; white-space: nowrap; flex-shrink: 0;
}
.task.pending .status-badge { background: rgba(240,198,116,0.15); color: var(--status-pending); }
.task.done .status-badge    { background: rgba(0,212,170,0.15); color: var(--status-done); }
.task.failed .status-badge  { background: rgba(255,107,107,0.15); color: var(--status-failed); }
.task.pending-hitl {
  background: rgba(255,159,67,0.08);
  border-left: 3px solid var(--status-hitl);
}
.task.pending-hitl .status-badge { background: rgba(255,159,67,0.2); color: var(--status-hitl); }
.task.pending-hitl .desc { font-weight: 600; }
.task .desc { flex: 1; word-break: break-word; }
.task .meta { font-size: 10px; color: var(--muted); }

.hitl-banner {
  margin: 0; padding: 10px 14px;
  background: rgba(255,159,67,0.1); border-bottom: 1px solid var(--status-hitl);
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  color: var(--status-hitl); font-weight: 600;
  letter-spacing: 0.06em; text-transform: uppercase;
}
.hitl-row {
  padding: 10px 14px; border-bottom: 1px solid var(--border);
  background: rgba(255,159,67,0.04);
}
.hitl-row .prompt {
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  margin-bottom: 8px; word-break: break-word;
}
.hitl-row .controls { display: flex; gap: 6px; align-items: center; }
.hitl-row input[type=text] { flex: 1; }

.chat-history {
  flex: 1; overflow-y: auto; padding: 14px 18px;
  display: flex; flex-direction: column; gap: 12px;
}
.msg {
  padding: 10px 12px; border-radius: 6px; max-width: 90%;
  font-size: 13px; line-height: 1.5; word-wrap: break-word; white-space: pre-wrap;
}
.msg.user { background: var(--surface-2); border: 1px solid var(--border); align-self: flex-end; }
.msg.assistant { background: rgba(74,158,255,0.07); border: 1px solid rgba(74,158,255,0.25); align-self: flex-start; }
.msg.system { background: rgba(0,212,170,0.05); border: 1px dashed var(--accent-green); color: var(--muted); font-size: 11px; align-self: stretch; font-family: 'IBM Plex Mono', monospace; }

.chat-input-row {
  display: flex; gap: 6px; padding: 10px 14px;
  border-top: 1px solid var(--border); background: var(--surface);
}
.chat-input-row textarea {
  flex: 1; background: var(--surface-2); border: 1px solid var(--border);
  color: var(--text); padding: 8px 10px; border-radius: 4px;
  font-family: 'IBM Plex Sans', sans-serif; font-size: 13px;
  resize: none; max-height: 160px; min-height: 36px;
}

.toast {
  position: fixed; bottom: 16px; right: 16px; z-index: 100;
  background: var(--surface-2); border: 1px solid var(--border);
  padding: 10px 14px; border-radius: 4px; font-size: 12px;
  font-family: 'IBM Plex Mono', monospace;
  display: none;
}
.toast.show { display: block; }
.toast.error { border-color: var(--status-failed); color: var(--status-failed); }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="sidebar-header">MERIDIAN<small>v0.2.0 dashboard</small></div>
    <div id="api-warn">ANTHROPIC_API_KEY not set — chat disabled.</div>
    <div class="projects-label">Projects</div>
    <div id="project-list" class="project-list"></div>
    <div class="new-project">
      <input id="new-project-name" type="text" placeholder="new project name">
      <button class="primary" id="new-project-btn">+</button>
    </div>
  </aside>
  <main class="main">
    <div class="tabs" id="tabs"></div>
    <div class="tab-bodies" id="tab-bodies">
      <div class="empty">no project open — pick one on the left</div>
    </div>
  </main>
</div>
<div class="toast" id="toast"></div>

<script>
const TABS_KEY = 'meridian.openTabs';
const state = {
  projects: [],
  tabs: [], // [{id, project}]
  activeTab: null,
  panels: {}, // tabId -> { ws, taskCache, sessionName, chatHistory, goalRaw, goalIsJson }
  apiKeyConfigured: false,
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

async function loadConfig() {
  try {
    const cfg = await api('/config/api-key');
    state.apiKeyConfigured = !!cfg.configured;
    document.getElementById('api-warn').style.display = cfg.configured ? 'none' : 'block';
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
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
}

function buildTabBody(project) {
  const root = document.getElementById('tab-bodies');
  const empty = root.querySelector(':scope > .empty');
  if (empty) empty.remove();

  const body = document.createElement('div');
  body.className = 'tab-body';
  body.id = `tab-body-${project.id}`;
  body.innerHTML = `
    <section class="panel left">
      <div class="panel-header">
        <span>STATUS · ${escapeHtml(project.name)}</span>
        <span><span class="ws-dot" id="ws-${project.id}"></span></span>
      </div>
      <div class="section">
        <h3>Goal State <span class="goal-version" id="goal-version-${project.id}"></span></h3>
        <textarea class="goal-area mono" id="goal-${project.id}" placeholder="(no goal set)"></textarea>
        <div class="goal-actions">
          <button class="primary" id="save-goal-${project.id}">save</button>
          <span class="goal-version" id="goal-state-${project.id}"></span>
        </div>
      </div>
      <div class="section">
        <h3>Active Sessions</h3>
        <div class="sessions-list" id="sessions-${project.id}"></div>
      </div>
      <div class="hitl-banner" id="hitl-banner-${project.id}" style="display:none">HITL queue</div>
      <div id="hitl-queue-${project.id}"></div>
      <div class="section" style="border-bottom:none">
        <h3>Task Log</h3>
      </div>
      <div class="scroll-area"><div class="task-list" id="tasks-${project.id}"></div></div>
    </section>
    <section class="panel right">
      <div class="panel-header">
        <span>CHAT · claude-sonnet-4</span>
      </div>
      <div class="chat-history" id="chat-${project.id}"></div>
      <div class="chat-input-row">
        <textarea id="chat-input-${project.id}" placeholder="message claude (enter to send, shift+enter for newline)"></textarea>
        <button class="primary" id="chat-send-${project.id}">send</button>
      </div>
    </section>
  `;
  root.appendChild(body);

  state.panels[project.id] = {
    ws: null, taskCache: [], goalRaw: null, goalIsJson: false,
    chatHistory: [],
  };

  document.getElementById(`save-goal-${project.id}`).onclick = () => saveGoal(project.id);
  document.getElementById(`goal-${project.id}`).addEventListener('blur', () => saveGoal(project.id));
  document.getElementById(`chat-send-${project.id}`).onclick = () => sendChat(project.id);
  const input = document.getElementById(`chat-input-${project.id}`);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(project.id); }
  });

  refreshTab(project.id);
  connectWs(project.id);
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
  } catch (e) {
    ta.value = '';
    v.textContent = '(unset)';
  }
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
    toast('goal saved');
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
      `<div class="session-row"><span class="name">${escapeHtml(s.name)}</span><span class="meta">${escapeHtml(s.status)} · ${escapeHtml(s.last_seen)}</span></div>`
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
  return `
    <div class="task ${t.status}">
      <span class="status-badge">${t.status}</span>
      <div>
        <div class="desc">${escapeHtml(t.description)}</div>
        <div class="meta">${escapeHtml(t.created_at)}</div>
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
  if (!state.apiKeyConfigured) { toast('ANTHROPIC_API_KEY not set', true); return; }
  input.value = '';
  const panel = state.panels[projectId];
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
          if (obj.error) { acc += `\n[error: ${obj.error}]`; }
          else if (obj.delta) { acc += obj.delta; }
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
  const name = inp.value.trim();
  if (!name) return;
  try {
    const p = await api('/projects', { method: 'POST', body: JSON.stringify({ name }) });
    inp.value = '';
    await loadProjects();
    openTab(p);
  } catch(e) { toast('create failed: ' + e.message, true); }
};

async function restoreTabs() {
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem(TABS_KEY) || '[]'); } catch(e){}
  for (const id of saved) {
    const p = state.projects.find(x => x.id === id);
    if (p) openTab(p);
  }
}

(async function init() {
  await loadConfig();
  await loadProjects();
  await restoreTabs();
  // Periodic session refresh on the active tab — sessions don't generate
  // pub/sub events so polling fills that gap.
  setInterval(() => {
    if (state.activeTab) refreshSessions(state.activeTab);
  }, 10000);
})();
</script>
</body>
</html>
"""
