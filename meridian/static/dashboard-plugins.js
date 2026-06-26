// dashboard-plugins.js — Tunnel plugin management UI.
// Extracted from dashboard.js (sprint item b11c2d8b).
// Contains: tunnel slot config, curated plugin list, custom plugins, install
// cards, and the live registry browser (sprint item 9b288b91).
// Prerequisite for sprint items 56cb5d33 (three-state lifecycle) and 9b288b91 (live registry).

// Tunnel Plugins — per-account (tenant) config for what `meridian --tunnel`
// spawns behind each of the three transport slots (filesystem / code-intel /
// code-extractor). Swapping a slot's command (e.g. code-intel → codegraph) or
// disabling a slot is a pure config change here — no redeploy. Rendered under
// Settings, below Executor Rules. The config is account-scoped, so projectId is
// only used to locate the settings DOM host.
const _TUNNEL_DEFAULT_PORTS = { fs: 8808, code: 8809, extract: 8810, ppt: 8811, word: 8812, dc: 8813 };

// Opt-in slots (Office + Desktop Commander) ship disabled and their launcher is
// not bundled, so a fresh account shows them "not connected" with no obvious next
// step. These per-slot hints give a clear path to fix: the exact launcher command
// + what it needs. Rendered under the slot row only while it isn't connected.
const _OPTIN_SLOT_HINTS = {
  word: { pkg: 'uvx word-mcp-live', note: 'Live Word editing with tracked changes — needs uv (uvx).' },
  ppt: { pkg: 'uvx powerpoint-mcp', note: 'PowerPoint authoring — needs uv (uvx).' },
  dc: { pkg: 'npx -y @wonderwhy-er/desktop-commander@latest', note: 'Desktop Commander, local only — needs Node (npx).' },
};

// Curated, well-known MCP servers a user can drop into a tunnel slot's command.
// Copy-to-clipboard for now (one-click custom-slot install lands in a later
// sprint). Commands use the canonical `uvx` / `npx -y` launchers the tunnel
// already knows how to spawn.
const _CURATED_TUNNEL_PLUGINS = [
  { name: 'Sequential Thinking', command: 'npx -y @modelcontextprotocol/server-sequential-thinking', description: 'Structured step-by-step reasoning', docs: 'https://github.com/modelcontextprotocol/servers' },
  { name: 'Fetch', command: 'uvx mcp-server-fetch', description: 'Fetch & convert web pages to markdown', docs: 'https://github.com/modelcontextprotocol/servers' },
  { name: 'Git', command: 'uvx mcp-server-git', description: 'Read/search/manipulate Git repos', docs: 'https://github.com/modelcontextprotocol/servers' },
  { name: 'Time', command: 'uvx mcp-server-time', description: 'Time & timezone conversion', docs: 'https://github.com/modelcontextprotocol/servers' },
  { name: 'Memory', command: 'npx -y @modelcontextprotocol/server-memory', description: 'Knowledge-graph persistent memory', docs: 'https://github.com/modelcontextprotocol/servers' },
];
window._CURATED_TUNNEL_PLUGINS = _CURATED_TUNNEL_PLUGINS;

// Detect the viewer's OS so we can surface the right dependency-install commands
// for the two launchers tunnel plugins use (uv → uvx, Node → npx). Returns one
// of 'windows' | 'macos' | 'linux' (best-effort; defaults to 'linux').
function _detectTunnelOs() {
  const ua = (navigator.userAgent || '') + ' ' + (navigator.platform || '');
  if (/win/i.test(ua)) return 'windows';
  if (/mac|darwin|iphone|ipad/i.test(ua)) return 'macos';
  return 'linux';
}
window._detectTunnelOs = _detectTunnelOs;

// uv (powers `uvx`) + Node.js (powers `npx`) install one-liners per OS.
const _TUNNEL_INSTALL_CMDS = {
  windows: {
    label: 'Windows',
    uv: 'winget install --id=astral-sh.uv -e',
    node: 'winget install OpenJS.NodeJS -e',
  },
  macos: {
    label: 'macOS',
    uv: 'brew install uv',
    node: 'brew install node',
  },
  linux: {
    label: 'Linux',
    uv: 'curl -LsSf https://astral.sh/uv/install.sh | sh',
    node: 'sudo apt-get install -y nodejs npm',
  },
};
window._TUNNEL_INSTALL_CMDS = _TUNNEL_INSTALL_CMDS;

// Copy text to the clipboard with a graceful fallback for non-secure contexts
// or browsers without the async Clipboard API.
async function _tunnelCopyToClipboard(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through to legacy path */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (_) {
    return false;
  }
}
window._tunnelCopyToClipboard = _tunnelCopyToClipboard;

// 9a8645c1 — render an actionable yellow warning for a slot the client reported
// unhealthy (slot_status[slot] = {reason, detail}). Returns '' when healthy.
// Pure + exported so the UI test can exercise it directly.
function _renderSlotHealthWarning(slot, slotStatus) {
  const st = slotStatus && slotStatus[slot];
  if (!st || (!st.reason && !st.detail)) return '';
  const label = st.reason === 'access_denied' ? 'access denied'
    : (st.reason || 'unavailable').replace(/_/g, ' ');
  const detail = st.detail ? `<div style="margin-top:3px;color:var(--muted);font-weight:400">${escapeHtml(st.detail)}</div>` : '';
  return `
        <div data-slot-warning="${escapeHtml(slot)}" style="margin-top:6px;padding:6px 8px;border:1px solid #c79a00;border-radius:4px;background:rgba(199,154,0,0.10);font-size:9px;line-height:1.6;color:#e0b400">
          <span style="font-weight:700">&#9888; ${escapeHtml(label)}</span>${detail}
        </div>`;
}
window._renderSlotHealthWarning = _renderSlotHealthWarning;

async function loadTunnelPluginsSection(projectId) {
  const host = document.getElementById(`settings-body-${projectId}`);
  if (!host) return;
  const existing = document.getElementById(`tunnel-plugins-section-${projectId}`);
  if (existing) existing.remove();
  const section = document.createElement('div');
  section.id = `tunnel-plugins-section-${projectId}`;
  section.style.cssText = 'margin-top:18px;padding-top:14px;border-top:1px solid var(--border)';
  host.appendChild(section);

  try {
    const data = await api('/tunnel/plugins');
    // The tunnel is a Pro/admin feature — only show this card to those plans.
    const plan = (data && data.plan) || 'free';
    if (!(plan === 'pro' || plan === 'admin' || (data && data.is_admin))) {
      section.remove();
      return;
    }
    const plugins = (data && data.plugins) || [];
    const active = (data && data.active) || {};
    // User-defined (non-built-in) plugins — LOCAL-ONLY: they ride a local
    // mcp-proxy port + the local .mcp.json, never the claude.ai connector.
    // Kept in a mutable array so Add/Remove re-render without a round-trip;
    // collectConfig() merges them into the PUT body alongside the slot overrides.
    const customPlugins = ((data && data.custom) || []).map((c) => ({
      name: String(c.name || ''),
      command: Array.isArray(c.command) ? c.command.join(' ') : String(c.command || ''),
      port: c.port,
      enabled: c.enabled !== false,
    }));

    // 9a8645c1 — per-slot health diagnostics (e.g. Serena access-denied) so a
    // degraded slot shows an actionable yellow warning instead of a silent dot.
    const slotStatus = (data && data.slot_status) || {};
    const renderRow = (p) => {
      const cmd = Array.isArray(p.command) ? p.command.join(' ') : '';
      // Three-state lifecycle (sprint item 56cb5d33):
      // active → tunnel running and slot connected
      // installed_inactive → enabled in config but tunnel not running
      // not_installed → disabled or binary not available
      const lifecycleState = _pluginLifecycleState(p, active);
      // Derive install command from opt-in hint or existing command.
      const hint = _OPTIN_SLOT_HINTS[p.slot];
      const installCmd = hint ? hint.pkg : (cmd || '');
      const lifecycleBadge = _renderLifecycleBadge(p, lifecycleState, installCmd);
      const hintHtml = (hint && lifecycleState === 'not_installed') ? `
          <div style="margin-top:6px;font-size:9px;color:var(--muted);line-height:1.6">
            Enable the toggle, then restart <code style="font-family:var(--font-mono)">meridian --tunnel</code> to launch
            <code style="font-family:var(--font-mono)">${escapeHtml(hint.pkg)}</code>.<br>${escapeHtml(hint.note)}
          </div>` : '';
      // 9a8645c1 — yellow warning badge when the client reported this slot
      // unhealthy with a reason (e.g. Serena access-denied).
      const warnHtml = _renderSlotHealthWarning(p.slot, slotStatus);
      // Core tools are always-on: show a locked "core" badge instead of an enable
      // toggle. Plugins keep the checkbox. collectConfig keys off .tp-command (on
      // every row), so a core slot's command/port override still saves. (b2a60de7)
      const toggle = p.core
        ? `<span title="core tool — always on" style="font-size:8px;font-weight:700;letter-spacing:.3px;color:var(--muted);border:1px solid var(--border);border-radius:3px;padding:1px 5px;text-transform:uppercase">core</span>`
        : `<input type="checkbox" class="tp-enabled" data-name="${escapeHtml(p.name)}" ${p.enabled ? 'checked' : ''}
                style="width:14px;height:14px;accent-color:var(--accent);cursor:pointer">`;
      return `
        <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:8px" data-lifecycle="${lifecycleState}">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <label style="display:flex;align-items:center;gap:8px;cursor:${p.core ? 'default' : 'pointer'};font-size:11px;color:var(--text);font-weight:600">
              ${toggle}
              ${escapeHtml(p.name)}
              <span style="font-size:9px;color:var(--muted);font-weight:400">/${escapeHtml(p.slot)}</span>
            </label>
            ${lifecycleBadge}
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <input type="text" class="tp-command" data-name="${escapeHtml(p.name)}" value="${escapeHtml(cmd)}"
              placeholder="default (${escapeHtml(p.description || 'built-in command')})"
              style="flex:1;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="number" class="tp-port" data-name="${escapeHtml(p.name)}" data-slot="${escapeHtml(p.slot)}" value="${p.port}"
              title="local proxy port"
              style="width:74px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
          </div>
          ${warnHtml}
          ${hintHtml}
          <details class="tp-tools" data-slot="${escapeHtml(p.slot)}" data-loaded="0" style="margin-top:6px">
            <summary style="cursor:pointer;list-style:none;font-size:10px;color:var(--accent);user-select:none">&#9656; tools</summary>
            <div class="tp-tools-body" style="margin-top:5px;font-size:10px;color:var(--muted);font-family:var(--font-mono)">&hellip;</div>
          </details>
        </div>`;
    };
    // Split the slots into always-on Core Tools and opt-in Plugins. (b2a60de7)
    const coreRows = plugins.filter((p) => p.core).map(renderRow).join('');
    const pluginRows = plugins.filter((p) => !p.core).map(renderRow).join('');
    const _sectionLabel = (text, note) =>
      `<div style="font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin:2px 0 6px">${text} <span style="font-weight:400;text-transform:none">${note}</span></div>`;
    const rows = `
      ${coreRows ? _sectionLabel('Core Tools', '— always on') + coreRows : ''}
      ${_sectionLabel('Plugins', '— opt-in, toggle to enable')}
      ${pluginRows || '<div style="color:var(--muted);font-size:10px">No plugins.</div>'}`;

    const detectedOs = _detectTunnelOs();
    const installCard = (label, cmds, prominent) => `
      <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;background:var(--surface-1)${prominent ? '' : ';opacity:.85'}">
        <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:6px">${escapeHtml(label)}${prominent ? ' <span style="color:var(--muted);font-weight:400">(detected)</span>' : ''}</div>
        ${[['uv', 'powers uvx plugins', cmds.uv], ['Node.js', 'powers npx plugins', cmds.node]].map(([dep, note, command]) => `
          <div style="margin-bottom:6px">
            <div style="font-size:9px;color:var(--muted);margin-bottom:3px">${escapeHtml(dep)} <span style="opacity:.8">— ${escapeHtml(note)}</span></div>
            <div style="display:flex;gap:6px;align-items:center">
              <code style="flex:1;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 7px;overflow-x:auto;white-space:nowrap">${escapeHtml(command)}</code>
              <button class="secondary tp-copy" data-copy="${escapeHtml(command)}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Copy</button>
            </div>
          </div>`).join('')}
      </div>`;

    const otherOsCards = Object.keys(_TUNNEL_INSTALL_CMDS)
      .filter((k) => k !== detectedOs)
      .map((k) => installCard(_TUNNEL_INSTALL_CMDS[k].label, _TUNNEL_INSTALL_CMDS[k], false))
      .join('');

    const installSection = `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Install dependencies</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Tunnel plugins launch via <code>uvx</code> (uv) and <code>npx</code> (Node.js). Install whichever a plugin's command needs.
          </div>
          ${installCard(_TUNNEL_INSTALL_CMDS[detectedOs].label, _TUNNEL_INSTALL_CMDS[detectedOs], true)}
          <details style="margin-top:2px">
            <summary style="cursor:pointer;list-style:none;font-size:10px;color:var(--muted)">&#9656; other platforms</summary>
            <div style="margin-top:6px">${otherOsCards}</div>
          </details>
        </div>
      </details>`;

    // Browse section — live registry browser or curated fallback (sprint item 9b288b91).
    const browseSection = await _renderPluginBrowseSection(projectId);

    // Custom plugins subsection. Renders the user-defined plugins list (each with
    // a Remove button) + an add form (name / command / port). LOCAL-ONLY: each
    // runs as a local mcp-proxy and is written into the local .mcp.json — they
    // never appear in the claude.ai connector. The list lives in `customPlugins`
    // and is re-rendered into #tp-custom-list-${projectId} on every Add/Remove.
    const renderCustomList = () => {
      const listEl = document.getElementById(`tp-custom-list-${projectId}`);
      if (!listEl) return;
      if (!customPlugins.length) {
        listEl.innerHTML = '<div style="color:var(--muted);font-size:10px">No custom plugins yet.</div>';
        return;
      }
      listEl.innerHTML = customPlugins.map((c, i) => `
        <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;display:flex;gap:8px;align-items:center">
          <div style="flex:1;min-width:0">
            <div style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(c.name)}
              <span style="font-size:9px;color:var(--muted);font-weight:400">:${escapeHtml(String(c.port))}</span></div>
            <div style="font-size:10px;color:var(--muted);font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(c.command)}</div>
          </div>
          <button class="secondary tp-custom-remove" data-idx="${i}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Remove</button>
        </div>`).join('');
      listEl.querySelectorAll('.tp-custom-remove').forEach((btn) => {
        btn.addEventListener('click', () => {
          const idx = parseInt(btn.dataset.idx, 10);
          if (Number.isInteger(idx)) { customPlugins.splice(idx, 1); renderCustomList(); }
        });
      });
    };

    const customSection = `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Custom plugins</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Add your own MCP server. Runs locally as <code>http://127.0.0.1:&lt;port&gt;</code> and is written
            into this machine's <code>.mcp.json</code> for a co-located Cursor / Claude Code session.
            Local-only — it does not appear in the claude.ai connector. Use a port outside 8808–8813.
          </div>
          <div id="tp-custom-list-${projectId}" style="margin-bottom:8px"></div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <input type="text" id="tp-custom-name-${projectId}" placeholder="name (e.g. fetch)"
              style="width:120px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="text" id="tp-custom-command-${projectId}" placeholder="command (e.g. uvx mcp-server-fetch)"
              style="flex:1;min-width:160px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="number" id="tp-custom-port-${projectId}" placeholder="port"
              style="width:74px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <button class="secondary admin-only" id="tp-custom-add-${projectId}" style="padding:2px 10px;font-size:10px;flex-shrink:0">Add</button>
          </div>
        </div>
      </details>`;

    section.innerHTML = `
      <details class="meridian-disclosure" open style="border:1px solid var(--border);border-radius:6px;background:var(--surface-2);padding:0">
      <summary style="cursor:pointer;list-style:none;padding:8px 10px;font-size:11px;font-weight:700;letter-spacing:.5px;color:var(--accent);text-transform:uppercase">Tunnel Plugins</summary>
      <div style="padding:0 10px 10px">
      <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
        What <code>meridian --tunnel</code> spawns behind each transport slot. Leave a command
        blank for the built-in default, or set one to swap it (e.g. <code>code-intel</code> →
        <code>codegraph</code>). Changes apply the next time the tunnel restarts.
      </div>
      ${rows || '<div style="color:var(--muted);font-size:10px">No plugins.</div>'}
      <div style="display:flex;justify-content:flex-end;gap:6px;margin-top:6px">
        <button class="secondary admin-only" id="tp-reset-${projectId}" style="padding:2px 10px;font-size:10px" title="Clear all overrides (back to built-in defaults)">Reset to defaults</button>
        <button class="primary admin-only" id="tp-save-${projectId}" style="padding:2px 10px;font-size:10px">Save</button>
      </div>
      <div id="tp-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:4px;text-align:right"></div>
      ${customSection}
      ${installSection}
      ${browseSection}
      </div>
      </details>`;

    const statusEl = document.getElementById(`tp-status-${projectId}`);
    const setStatus = (m) => { if (statusEl) { statusEl.textContent = m; if (m) setTimeout(() => { if (statusEl.textContent === m) statusEl.textContent = ''; }, 2500); } };

    const collectConfig = () => {
      const cfg = [];
      // Iterate .tp-command (present on every built-in row) rather than .tp-enabled
      // (core rows have no checkbox) so a core slot's command/port override still
      // persists. A core row with no override + no enabled toggle is skipped.
      section.querySelectorAll('.tp-command').forEach((cmdEl) => {
        const name = cmdEl.dataset.name;
        const portEl = section.querySelector(`.tp-port[data-name="${CSS.escape(name)}"]`);
        const enEl = section.querySelector(`.tp-enabled[data-name="${CSS.escape(name)}"]`);
        const entry = { name };
        if (enEl) entry.enabled = enEl.checked;  // plugins only; core stays default-on
        const cmdVal = (cmdEl.value || '').trim();
        if (cmdVal) entry.command = cmdVal;
        const portVal = parseInt(portEl && portEl.value, 10);
        const slot = portEl && portEl.dataset.slot;
        if (Number.isInteger(portVal) && portVal !== _TUNNEL_DEFAULT_PORTS[slot]) entry.port = portVal;
        // Skip empty core rows (name only — no override, no toggle).
        if (entry.command !== undefined || entry.port !== undefined || entry.enabled !== undefined) {
          cfg.push(entry);
        }
      });
      // Merge in the user-defined custom plugins (name + command + port + enabled).
      // The server keeps non-built-in names, so these round-trip back as data.custom.
      customPlugins.forEach((c) => {
        const name = (c.name || '').trim();
        const command = (c.command || '').trim();
        const port = parseInt(c.port, 10);
        if (!name || !command || !Number.isInteger(port)) return;
        cfg.push({ name, command, port, enabled: c.enabled !== false });
      });
      return cfg;
    };

    document.getElementById(`tp-save-${projectId}`).onclick = async () => {
      try {
        await api('/tunnel/plugins', { method: 'PUT', body: JSON.stringify({ config: collectConfig() }) });
        toast('Tunnel plugins saved');
        setStatus('Saved — restart the tunnel to apply.');
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };

    document.getElementById(`tp-reset-${projectId}`).onclick = async () => {
      if (!confirm('Reset tunnel plugins?\n\nThis clears ALL command and port overrides for every slot and returns them to Meridian\'s built-in defaults. This cannot be undone.')) return;
      try {
        await api('/tunnel/plugins', { method: 'PUT', body: JSON.stringify({ config: [] }) });
        toast('Reset to defaults');
        loadTunnelPluginsSection(projectId);
      } catch (e) { toast('Reset failed: ' + e.message, true); }
    };

    // Custom plugins: initial render + Add-form wiring.
    renderCustomList();
    const _addCustom = () => {
      const nameEl = document.getElementById(`tp-custom-name-${projectId}`);
      const cmdEl = document.getElementById(`tp-custom-command-${projectId}`);
      const portEl = document.getElementById(`tp-custom-port-${projectId}`);
      const name = (nameEl && nameEl.value || '').trim();
      const command = (cmdEl && cmdEl.value || '').trim();
      const port = parseInt(portEl && portEl.value, 10);
      if (!name || !command || !Number.isInteger(port)) {
        toast('Custom plugin needs a name, command, and port', true);
        return;
      }
      if (port < 1024 || port > 65535 || [8808, 8809, 8810, 8811, 8812, 8813].includes(port)) {
        toast('Pick a port in 1024–65535 and outside 8808–8813', true);
        return;
      }
      if (customPlugins.some((c) => c.name === name)) {
        toast(`A custom plugin named "${name}" already exists`, true);
        return;
      }
      customPlugins.push({ name, command, port, enabled: true });
      if (nameEl) nameEl.value = '';
      if (cmdEl) cmdEl.value = '';
      if (portEl) portEl.value = '';
      renderCustomList();
    };
    const _addBtn = document.getElementById(`tp-custom-add-${projectId}`);
    if (_addBtn) _addBtn.addEventListener('click', _addCustom);

    // Copy buttons (install commands + curated plugin commands).
    section.querySelectorAll('.tp-copy').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const ok = await _tunnelCopyToClipboard(btn.dataset.copy || '');
        if (ok) {
          const prev = btn.textContent;
          btn.textContent = 'Copied';
          setTimeout(() => { btn.textContent = prev; }, 1200);
        } else {
          toast('Copy failed — select and copy manually', true);
        }
      });
    });

    // Wire up live registry copy buttons and search/load-more (sprint item 9b288b91).
    _wireRegistryCopyButtons(section);
    _wireRegistryBrowse(section, projectId);

    // Wire up three-state lifecycle Install buttons (sprint item 56cb5d33).
    _wireLifecycleInstallButtons(section);

    // Per-plugin live tools dropdown. Lazy-load the slot's tool manifest the
    // first time its <details> is expanded; reuse one /me lookup across slots.
    let _tenantIdPromise = null;
    const _getTenantId = () => {
      if (!_tenantIdPromise) {
        _tenantIdPromise = api('/me').then((m) => (m && m.tenant_id) || null).catch(() => null);
      }
      return _tenantIdPromise;
    };

    section.querySelectorAll('.tp-tools').forEach((det) => {
      det.addEventListener('toggle', async () => {
        if (!det.open || det.dataset.loaded === '1') return;
        det.dataset.loaded = '1';
        const slot = det.dataset.slot;
        const bodyEl = det.querySelector('.tp-tools-body');
        if (!bodyEl) return;
        // Not active? Don't even bother hitting the proxy.
        if (!active[slot]) {
          bodyEl.innerHTML = '<span style="color:var(--muted)">not connected — start the tunnel</span>';
          det.dataset.loaded = '0';
          return;
        }
        bodyEl.textContent = 'loading…';
        try {
          const tenantId = await _getTenantId();
          if (!tenantId) throw new Error('no tenant');
          const r = await fetch(`/${slot}/mcp/${tenantId}/mcp`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream' },
            body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} }),
          });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const text = await r.text();
          let parsed = null;
          if (text.trim().startsWith('{')) {
            parsed = JSON.parse(text);
          } else {
            for (const line of text.split('\n')) {
              if (line.startsWith('data:')) {
                try { parsed = JSON.parse(line.slice(5).trim()); } catch (_) {}
              }
            }
          }
          if (!parsed) throw new Error('empty response');
          if (parsed.error) throw new Error(parsed.error.message || String(parsed.error));
          const tools = (parsed.result && parsed.result.tools) || [];
          if (!tools.length) {
            bodyEl.innerHTML = '<span style="color:var(--muted)">no tools reported</span>';
            return;
          }
          bodyEl.innerHTML = `<div style="color:var(--muted);margin-bottom:3px">${tools.length} tool${tools.length !== 1 ? 's' : ''}</div>` +
            tools.map((t) => `<div style="color:var(--text)">${escapeHtml(t && t.name || String(t))}</div>`).join('');
        } catch (e) {
          bodyEl.innerHTML = `<span style="color:var(--muted)">not connected — start the tunnel</span>`;
          det.dataset.loaded = '0';
        }
      });
    });
  } catch (e) {
    section.innerHTML = `<div class="empty" style="color:var(--error)">Failed to load tunnel plugins: ${escapeHtml(e.message)}</div>`;
  }
}
window.loadTunnelPluginsSection = loadTunnelPluginsSection;

// ---------------------------------------------------------------------------
// Plugin Browse Section — sprint item 9b288b91
// Replaces the hardcoded _CURATED_TUNNEL_PLUGINS list with live data from the
// official MCP Registry API (registry.modelcontextprotocol.io/v0/servers),
// proxied through Meridian to avoid CORS. Falls back to the curated list if
// the proxy is unavailable (self-hosted without internet, etc.).
// ---------------------------------------------------------------------------

async function _renderPluginBrowseSection(projectId) {
  let servers = null;
  let nextCursor = null;

  try {
    const data = await api('/tunnel/registry?limit=20');
    if (data && Array.isArray(data.servers)) {
      servers = data.servers;
      nextCursor = data.next_cursor || null;
    }
  } catch (_) { /* fall back to curated list */ }

  if (!servers) {
    // Fallback: render the static curated list.
    const curatedRows = _CURATED_TUNNEL_PLUGINS.map((c) => `
      <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;background:var(--surface-1)">
        <div style="display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;margin-bottom:4px">
          <span style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(c.name)}</span>
          <a href="${escapeHtml(c.docs)}" target="_blank" rel="noopener" style="font-size:9px;color:var(--accent);text-decoration:none">docs &#8599;</a>
        </div>
        <div style="font-size:10px;color:var(--muted);margin-bottom:5px">${escapeHtml(c.description)}</div>
        <div style="display:flex;gap:6px;align-items:center">
          <code style="flex:1;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 7px;overflow-x:auto;white-space:nowrap">${escapeHtml(c.command)}</code>
          <button class="secondary tp-copy" data-copy="${escapeHtml(c.command)}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Copy command</button>
        </div>
      </div>`).join('');

    return `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Browse plugins</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Well-known MCP servers. Copy a command and paste it into a slot above to swap that slot's launcher.
          </div>
          ${curatedRows}
        </div>
      </details>`;
  }

  // Live registry — render server cards.
  const _renderRegistryCard = (s) => {
    const name = escapeHtml(s.name || s.id || '');
    const desc = escapeHtml(s.description || '');
    const installCmd = s.install_command || '';
    const homepage = escapeHtml(s.homepage || s.url || '');
    return `
      <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;background:var(--surface-1)">
        <div style="display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;margin-bottom:4px">
          <span style="font-size:11px;color:var(--text);font-weight:600">${name}</span>
          ${homepage ? `<a href="${homepage}" target="_blank" rel="noopener" style="font-size:9px;color:var(--accent);text-decoration:none">docs &#8599;</a>` : ''}
        </div>
        ${desc ? `<div style="font-size:10px;color:var(--muted);margin-bottom:5px">${desc}</div>` : ''}
        ${installCmd ? `<div style="display:flex;gap:6px;align-items:center">
          <code style="flex:1;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 7px;overflow-x:auto;white-space:nowrap">${escapeHtml(installCmd)}</code>
          <button class="secondary rg-copy" data-copy="${escapeHtml(installCmd)}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Copy command</button>
        </div>` : ''}
      </div>`;
  };
  window._renderRegistryCard = _renderRegistryCard;

  const serverRows = servers.map(_renderRegistryCard).join('');
  const loadMoreAttr = nextCursor ? `data-cursor="${escapeHtml(nextCursor)}"` : '';
  const loadMoreBtn = nextCursor
    ? `<button class="secondary" id="rg-load-more-${projectId}" ${loadMoreAttr} style="width:100%;font-size:10px;padding:4px 0;margin-top:4px">Load more</button>`
    : '';

  return `
    <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
      <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Browse plugins (live registry)</summary>
      <div style="padding:0 8px 8px">
        <div style="display:flex;gap:6px;margin-bottom:8px;align-items:center">
          <input type="text" id="rg-search-${projectId}" placeholder="Search MCP servers…"
            style="flex:1;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;padding:5px 7px;outline:none">
        </div>
        <div id="rg-list-${projectId}">${serverRows}</div>
        ${loadMoreBtn}
      </div>
    </details>`;
}
window._renderPluginBrowseSection = _renderPluginBrowseSection;

// Wire copy buttons with class .rg-copy (live registry server cards).
function _wireRegistryCopyButtons(container) {
  if (!container) return;
  container.querySelectorAll('.rg-copy').forEach((btn) => {
    if (btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.addEventListener('click', async () => {
      const ok = await _tunnelCopyToClipboard(btn.dataset.copy || '');
      if (ok) {
        const prev = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = prev; }, 1200);
      } else {
        toast('Copy failed — select and copy manually', true);
      }
    });
  });
}
window._wireRegistryCopyButtons = _wireRegistryCopyButtons;

// Wire up the live registry search filter and Load More button.
// Called after section.innerHTML is set in loadTunnelPluginsSection.
function _wireRegistryBrowse(section, projectId) {
  const searchEl = document.getElementById(`rg-search-${projectId}`);
  if (searchEl) {
    searchEl.addEventListener('input', () => {
      const q = searchEl.value.toLowerCase();
      const listEl = document.getElementById(`rg-list-${projectId}`);
      if (!listEl) return;
      listEl.querySelectorAll(':scope > div').forEach((row) => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(q) ? '' : 'none';
      });
    });
  }

  const loadMoreBtn = document.getElementById(`rg-load-more-${projectId}`);
  if (loadMoreBtn) {
    loadMoreBtn.addEventListener('click', async () => {
      const cursor = loadMoreBtn.dataset.cursor;
      if (!cursor) return;
      loadMoreBtn.textContent = 'Loading…';
      loadMoreBtn.disabled = true;
      try {
        const data = await api(`/tunnel/registry?limit=20&cursor=${encodeURIComponent(cursor)}`);
        if (data && Array.isArray(data.servers)) {
          const listEl = document.getElementById(`rg-list-${projectId}`);
          if (listEl) {
            data.servers.forEach((s) => {
              const tmp = document.createElement('div');
              tmp.innerHTML = window._renderRegistryCard ? window._renderRegistryCard(s) : '';
              while (tmp.firstChild) listEl.appendChild(tmp.firstChild);
            });
            _wireRegistryCopyButtons(listEl);
          }
          if (data.next_cursor) {
            loadMoreBtn.dataset.cursor = data.next_cursor;
            loadMoreBtn.textContent = 'Load more';
            loadMoreBtn.disabled = false;
          } else {
            loadMoreBtn.remove();
          }
        }
      } catch (e) {
        loadMoreBtn.textContent = 'Load more';
        loadMoreBtn.disabled = false;
        toast('Failed to load more: ' + e.message, true);
      }
    });
  }
}
window._wireRegistryBrowse = _wireRegistryBrowse;

// ---------------------------------------------------------------------------
// Plugin three-state lifecycle — sprint item 56cb5d33
// States: not_installed | installed_inactive | active
//
// - active:             tunnel is running and slot is connected (active[slot]=true)
// - installed_inactive: enabled in config but tunnel not connected / slot not active
// - not_installed:      binary check failed or plugin explicitly disabled
//
// The dashboard shows:
//   active            → green dot, "active" badge, Deactivate button
//   installed_inactive → yellow dot, "inactive" badge, Activate button
//   not_installed     → grey dot, "not installed" badge, Install button (copies cmd)
// ---------------------------------------------------------------------------

/**
 * Determine the three-state lifecycle for a plugin slot.
 * @param {object} plugin  - plugin descriptor from /tunnel/plugins response
 * @param {object} active  - map of slot → bool (connected sockets)
 * @returns {'active'|'installed_inactive'|'not_installed'}
 */
function _pluginLifecycleState(plugin, active) {
  if (active && active[plugin.slot]) return 'active';
  if (plugin.enabled !== false) return 'installed_inactive';
  return 'not_installed';
}
window._pluginLifecycleState = _pluginLifecycleState;

/**
 * Render the three-state lifecycle badge + action button for a plugin row.
 * Returns an HTML string. The Install button copies the launch command to
 * clipboard (self-hosted: also offers a server-side binary check).
 */
function _renderLifecycleBadge(plugin, lifecycleState, installCmd) {
  const styles = {
    active:             { dot: 'var(--success, #3fb950)', label: 'active',    labelColor: 'var(--success, #3fb950)' },
    installed_inactive: { dot: '#f59e0b',                 label: 'inactive',  labelColor: '#f59e0b' },
    not_installed:      { dot: 'var(--muted)',             label: 'not installed', labelColor: 'var(--muted)' },
  };
  const s = styles[lifecycleState] || styles.not_installed;
  const dotHtml = `<span style="width:8px;height:8px;border-radius:50%;background:${s.dot};flex-shrink:0"></span>`;
  const labelHtml = `<span style="font-size:9px;color:${s.labelColor};font-weight:600">${s.label}</span>`;

  let actionBtn = '';
  if (lifecycleState === 'not_installed' && installCmd) {
    const safeCmd = escapeHtml(installCmd);
    actionBtn = `<button class="secondary tp-install-btn" data-install-cmd="${safeCmd}" style="padding:2px 8px;font-size:10px;flex-shrink:0" title="Copy install command">Install</button>`;
  } else if (lifecycleState === 'installed_inactive') {
    actionBtn = `<span style="font-size:9px;color:var(--muted);font-style:italic">start tunnel to activate</span>`;
  }

  return `<span style="display:inline-flex;align-items:center;gap:4px">${dotHtml}${labelHtml}${actionBtn ? ' ' + actionBtn : ''}</span>`;
}
window._renderLifecycleBadge = _renderLifecycleBadge;

/**
 * Wire Install buttons (.tp-install-btn) inside a container.
 * Clicking copies the install command and optionally shows a check result.
 */
function _wireLifecycleInstallButtons(container) {
  if (!container) return;
  container.querySelectorAll('.tp-install-btn').forEach((btn) => {
    if (btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.addEventListener('click', async () => {
      const cmd = btn.dataset.installCmd || '';
      if (!cmd) return;
      const ok = await _tunnelCopyToClipboard(cmd);
      if (ok) {
        const prev = btn.textContent;
        btn.textContent = 'Copied!';
        btn.title = 'Paste in your terminal to install';
        setTimeout(() => { btn.textContent = prev; btn.title = 'Copy install command'; }, 2000);
      } else {
        toast('Copy failed — manual copy needed', true);
      }
    });
  });
}
window._wireLifecycleInstallButtons = _wireLifecycleInstallButtons;
