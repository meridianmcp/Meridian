export function suggestNtfyTopic(projectId) {

  const proj = (window.state?.projects || []).find(p => p.id === projectId);

  const slug = (proj?.name || 'meridian')

    .toLowerCase()

    .replace(/[^a-z0-9]+/g, '-')

    .replace(/^-+|-+$/g, '')

    .slice(0, 24) || 'meridian';

  return slug;

}

export async function loadSettingsTab(projectId) {

  const body = document.getElementById(`settings-body-${projectId}`);

  if (!body) return;

  body.innerHTML = '<div style="color:var(--muted);font-size:11px">loading…</div>';


  const PREFS = [

    { key: 'hitl',    label: 'HITL — get notified when a session needs your input' },

    { key: 'sprint',  label: 'Sprint done — all items completed' },

  ];



  // Fetch both in parallel; mcp-config 404 = self-hosted (skip section).

  try {

  const [notifResult, mcpResult, settingsResult, ntfyResult, ghResult] = await Promise.allSettled([

    api('/settings/notifications'),

    api('/settings/mcp-config'),

    loadProjectSettings(projectId),

    api(`/projects/${projectId}/ntfy`),

    api(`/projects/${projectId}/github/status`),

  ]);



  const prefs = (notifResult.status === 'fulfilled') ? (notifResult.value.prefs || {}) : null;

  const mcpData = (mcpResult.status === 'fulfilled') ? mcpResult.value : null;

  const projectSettings = (settingsResult.status === 'fulfilled')

    ? settingsResult.value

    : { project_id: projectId, max_pinned_decisions: DEFAULT_MAX_PINNED_DECISIONS };

  const ghData = (ghResult.status === 'fulfilled') ? ghResult.value : null;

  const ghRepos = Array.isArray(ghData?.repos) ? ghData.repos : [];

  const ghRepoMap = Object.fromEntries(ghRepos.map(repo => [repo.full_name, repo]));

  const ghSelectedRepo = ghData?.repo || ghRepos[0]?.full_name || '';

  const ghSelectedBranch = ghData?.branch || ghRepoMap[ghSelectedRepo]?.default_branch || 'main';

  const ghUsername = ghData?.github_user || ghData?.login || '';

  const ghAvatarUrl = ghData?.avatar_url || '';

  const ghRepoChoices = ghRepos.length

    ? ghRepos

    : (ghSelectedRepo

      ? [{

          full_name: ghSelectedRepo,

          name: ghSelectedRepo.split('/').slice(-1)[0] || ghSelectedRepo,

          owner: ghSelectedRepo.includes('/') ? ghSelectedRepo.split('/')[0] : '',

        }]

      : []);

  const repoGroups = new Map();

  ghRepoChoices.forEach(repo => {

    const fullName = repo.full_name || '';

    if (!fullName) return;

    const owner = repo.owner || (fullName.includes('/') ? fullName.split('/')[0] : 'other');

    if (!repoGroups.has(owner)) repoGroups.set(owner, []);

    repoGroups.get(owner).push(repo);

  });

  const ghRepoOptions = Array.from(repoGroups.keys())

    .sort((a, b) => {

      const aPersonal = !!ghUsername && a.toLowerCase() === ghUsername.toLowerCase();

      const bPersonal = !!ghUsername && b.toLowerCase() === ghUsername.toLowerCase();

      if (aPersonal !== bPersonal) return aPersonal ? -1 : 1;

      const aMeridian = a.toLowerCase() === 'meridianmcp';

      const bMeridian = b.toLowerCase() === 'meridianmcp';

      if (aMeridian !== bMeridian) return aMeridian ? -1 : 1;

      return a.localeCompare(b);

    })

    .map(owner => {

      const label = (!!ghUsername && owner.toLowerCase() === ghUsername.toLowerCase())

        ? `Personal (@${owner})`

        : `${owner} repos`;

      const options = (repoGroups.get(owner) || [])

        .slice()

        .sort((a, b) => String(a.name || a.full_name || '').localeCompare(String(b.name || b.full_name || '')))

        .map(repo => {

          const fullName = repo.full_name || '';

          const repoName = repo.name || (fullName.includes('/') ? fullName.split('/').slice(-1)[0] : fullName);

          return `<option value="${escapeHtml(fullName)}" ${fullName === ghSelectedRepo ? 'selected' : ''}>${escapeHtml(repoName)}</option>`;

        })

        .join('');

      return `<optgroup label="${escapeHtml(label)}">${options}</optgroup>`;

    })

    .join('');

  const hooksBaseUrl = ((mcpData && mcpData.base_url) || window.location.origin || window.state.serverConfig?.server_url || 'http://localhost:7878').replace(/\/$/, '');

  function buildHookCurlHeaders(token) {

    const headers = [];

    if (token) headers.push(`-H 'Authorization: Bearer ${token}'`);

    headers.push(`-H 'Content-Type: application/json'`);

    return headers.join(' ');

  }



  function buildHookCurlCommand(path, token) {

    const cmd = `curl -s -X POST ${buildHookCurlHeaders(token)} -d '{"project_id":"${projectId}"}' ${hooksBaseUrl}/hooks/${path}`;

    if (path === 'session-start') return `${cmd} | jq -r '.hookSpecificOutput.additionalContext // empty'`;

    return cmd;

  }



  function buildHookPowerShellCommand(path, token) {

    const headerClause = token ? ` -Headers @{ Authorization = 'Bearer ${token}' }` : '';

    const bodyJson = `{"project_id":"${projectId}"}`;

    if (path === 'session-start') {

      return `powershell -NoProfile -NonInteractive -Command "try { $r = Invoke-WebRequest -Method POST -Uri '${hooksBaseUrl}/hooks/session-start'${headerClause} -ContentType 'application/json' -Body '${bodyJson}' -UseBasicParsing; $r.Content } catch { '{}' }"`;

    }

    return `powershell -NoProfile -NonInteractive -Command "try { Invoke-WebRequest -Method POST -Uri '${hooksBaseUrl}/hooks/stop'${headerClause} -ContentType 'application/json' -Body '${bodyJson}' -UseBasicParsing | Out-Null } catch { }"`;

  }



  function buildClaudeHookSnippet(platform, token) {

    const start = platform === 'windows'

      ? buildHookPowerShellCommand('session-start', token)

      : buildHookCurlCommand('session-start', token);

    const stop = platform === 'windows'

      ? buildHookPowerShellCommand('stop', token)

      : buildHookCurlCommand('stop', token);

    return JSON.stringify({

      hooks: {

        SessionStart: [{ matcher: "", hooks: [{ type: 'command', command: start }] }],

        Stop: [{ matcher: "", hooks: [{ type: 'command', command: stop }] }],

      },

    }, null, 2);

  }



  function buildCodexHookSnippet(platform, token) {

    const start = platform === 'windows'

      ? buildHookPowerShellCommand('session-start', token)

      : buildHookCurlCommand('session-start', token);

    const stop = platform === 'windows'

      ? buildHookPowerShellCommand('stop', token)

      : buildHookCurlCommand('stop', token);

    return `[mcp_servers.meridian]\ntype = "http"\nurl = "${hooksBaseUrl}/mcp"\n\n[hooks]\nsession_start = ${JSON.stringify(start)}\nstop = ${JSON.stringify(stop)}`;

  }



  // Settings section accordion state — persisted per project
  let _secState = { connect: true, executor: false, config: false, account: false };
  try { Object.assign(_secState, JSON.parse(localStorage.getItem('meridian.settings.sections.' + projectId) || '{}')); } catch(e) {}
  const _secHtml = function(k, title) {
    const openAttr = _secState[k] ? 'open' : '';
    const caretRot = _secState[k] ? 'transform:rotate(90deg)' : '';
    return '<details id="settings-sec-' + k + '-' + projectId + '" ' + openAttr + ' style="margin-bottom:12px;border:1px solid var(--border);border-radius:6px">' +
      '<summary style="cursor:pointer;list-style:none;padding:10px 12px;display:flex;align-items:center;gap:8px">' +
      '<span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;' + caretRot + '">▶</span>' +
      '<span style="font-weight:600;font-size:11px;color:var(--text)">' + title + '</span>' +
      '</summary><div style="padding:0 0 4px">';
  };

  // Collect repo_paths across all projects for filesystem MCP snippet
  const _allRepoPaths = [];
  if (!isHostedMode()) {
    try {
      const _allProjSettings = await Promise.allSettled(
        (window.state.projects || []).map(p => loadProjectSettings(p.id))
      );
      for (const _ps of _allProjSettings) {
        if (_ps.status === 'fulfilled') {
          const _rps = Array.isArray(_ps.value?.executor_config?.repo_paths)
            ? _ps.value.executor_config.repo_paths : [];
          for (const _rp of _rps) {
            if (_rp && !_allRepoPaths.includes(_rp)) _allRepoPaths.push(_rp);
          }
        }
      }
    } catch (_) {}
  }

  let html = '';



  // G4.18 — Account section (hosted only). Shows email, plan, optional

  // workspace memberships, plus links for Manage billing (G2.11),

  // Sign out, and Delete account. Members-of and sign-out-everywhere

  // ride on existing endpoints; both are no-ops outside hosted mode.

  if (window.state.tenantEmail) {

    const plan = window.state.tenantPlan || 'free';

    const hasStripe = !!window.state.tenantHasStripe;

    // Admin / internal accounts: no billing UI. Stripe customers: POST portal
    // button (avoids GET redirect leak). Free tier: prominent upgrade link.

    const noUpgrade = plan === 'admin' || !!window.state.tenantIsInternal;

    let billingBtn = '';

    if (hasStripe) {

      billingBtn = `<button id="billing-portal-btn-${escapeHtml(projectId)}" class="primary" style="padding:4px 10px;font-size:10px;background:var(--accent);color:#001020;border-radius:4px;font-weight:600;cursor:pointer;border:none">Manage billing →</button>`;

    } else if (!noUpgrade) {

      const upgradeUrl = window.state.serverConfig?.stripe_payment_link || '/pricing';

      billingBtn = `<a href="${escapeHtml(upgradeUrl)}" class="primary" style="padding:4px 10px;font-size:10px;text-decoration:none;background:var(--accent);color:#001020;border-radius:4px;font-weight:600">Upgrade to Standard →</a>`;

    }

    // Trial / free-tier expiry line + resubscribe affordance. Only relevant to

    // plans with an inactivity expiry (free / trial); admin/internal/paid skip it.

    const days = window.state.tenantDaysRemaining;

    const expiresAt = window.state.tenantExpiresAt;

    const isTrialish = (plan === 'free' || plan === 'trial') && !window.state.tenantIsInternal;

    let expiryLine = '';

    let resubBtn = '';

    if (isTrialish && (expiresAt || days != null || window.state.tenantExpired)) {

      const dateStr = expiresAt ? String(expiresAt).slice(0, 10) : '';

      if (window.state.tenantExpired) {

        expiryLine = `<div style="color:#f87171">${_PLAN_LABELS[plan] || plan} expired${dateStr ? ` on ${escapeHtml(dateStr)}` : ''}.</div>`;

      } else {

        const dleft = (days != null) ? `${days} day${days === 1 ? '' : 's'} left` : '';

        expiryLine = `<div>${_PLAN_LABELS[plan] || plan} expires${dateStr ? ` on <span style="color:var(--text)">${escapeHtml(dateStr)}</span>` : ''}${dleft ? ` <span style="color:var(--muted)">(${dleft})</span>` : ''}.</div>`;

      }

      const payLink = window.state.serverConfig?.stripe_payment_link || '/pricing';

      resubBtn = `<a href="${escapeHtml(payLink)}" class="primary" style="padding:4px 10px;font-size:10px;text-decoration:none;background:var(--accent);color:#001020;border-radius:4px;font-weight:600">${window.state.tenantExpired ? 'Resubscribe' : 'Upgrade to Standard'}</a>`;

    }

    html += `<div data-demo-hide style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

      <div style="font-weight:600;font-size:11px;color:var(--text);margin-bottom:6px">Account</div>

      <div style="font-size:10px;color:var(--muted);line-height:1.7">

        <div>Email: <span style="color:var(--text)">${escapeHtml(window.state.tenantEmail)}</span></div>

        <div>Plan: <span style="color:var(--text)">${escapeHtml(_PLAN_LABELS[plan] || plan)}</span></div>

        ${expiryLine}

      </div>

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">

        ${resubBtn || billingBtn}

        <a href="/auth/logout" class="secondary" style="padding:4px 10px;font-size:10px;text-decoration:none;background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:4px">Sign out</a>

        <button id="account-delete-${projectId}" class="secondary" style="padding:4px 10px;font-size:10px;background:var(--surface-1);color:#f87171;border:1px solid #f8717155;border-radius:4px;cursor:pointer">Delete account…</button>

      </div>

    </div>`;

  }



  // Hosted easy-setup: hooks + mini executor shown by default; everything else in collapsible Advanced.
  if (isHostedMode()) {
    const _advKey = `meridian.settings.adv.${projectId}`;
    let _advOpen = false;
    try { _advOpen = localStorage.getItem(_advKey) === '1'; } catch(e) {}

    html += `<details class="meridian-disclosure" style="margin-bottom:16px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

    <summary style="cursor:pointer;list-style:none;padding:10px 12px;display:flex;justify-content:space-between;align-items:center;gap:8px">

      <span style="display:flex;align-items:center;gap:8px;flex:1;min-width:0">

        <span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;flex-shrink:0">▶</span>

        <span style="font-weight:600;font-size:11px;color:var(--text)">Meridian Connect</span>

      </span>

      <span style="font-size:10px;color:var(--muted);flex-shrink:0">Claude Code + Codex</span>

    </summary>

    <div style="padding:0 12px 12px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:10px">Install once per machine. Hooks auto-start sessions and sync context with Meridian.</div>

      ${osExecutorHintBanner(projectId)}

      <div style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));margin-bottom:10px">

        <div>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">macOS / Linux / WSL</div>

          <pre id="hooks-install-unix-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-install-unix-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

        <div>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Windows PowerShell</div>

          <pre id="hooks-install-windows-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-install-windows-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </div>

      <div style="font-size:10px;color:var(--muted);margin-top:6px">Need manual config? See <a href="https://docs.usemeridian.us/configuration" target="_blank" style="color:var(--accent);text-decoration:none">docs.usemeridian.us/configuration</a></div>

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;margin-top:10px">

        ${mcpData ? `<button id="hooks-gen-token-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Generate API key</button>` : ''}

        ${mcpData ? `<button id="hooks-gen-readonly-token-${projectId}" class="secondary" style="font-size:10px;padding:4px 10px" title="Read-only tokens can only call get_* tools — safe for ChatGPT connectors">Generate read-only key</button>` : ''}

        <span id="hooks-token-status-${projectId}" style="font-size:10px;color:var(--muted)">${mcpData ? 'Generate an API key to replace the placeholder token in the hosted snippets below.' : 'Local mode - no Bearer token needed.'}</span>

      </div>

      <div id="hooks-key-reveal-${projectId}" style="display:none;margin-bottom:8px;padding:8px 10px;border:1px solid var(--accent);border-radius:4px;background:var(--surface-1)">
        <div style="font-size:10px;color:var(--accent);font-weight:600;margin-bottom:6px">Your new API key — save it now, it won't be shown again:</div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <input id="hooks-key-reveal-input-${projectId}" type="text" readonly style="flex:1;min-width:180px;background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px">
          <button id="hooks-key-copy-btn-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Copy key</button>
          <button id="hooks-key-dismiss-${projectId}" class="secondary" style="font-size:10px;padding:4px 8px" title="Dismiss">×</button>
        </div>
      </div>

      ${mcpData ? `<div style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">

          <span style="font-size:10px;font-weight:600;color:var(--text)">Existing API keys</span>

          <button id="hooks-refresh-tokens-${projectId}" class="secondary" style="font-size:10px;padding:3px 8px">Refresh</button>

        </div>

        <div id="hooks-token-list-${projectId}" style="display:grid;gap:6px"></div>

      </div>` : ''}

      <details style="margin-bottom:10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">Windows manual config</summary>

        <div style="padding:0 10px 10px">

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:8px 0 4px">Claude Code - <code>~/.claude/settings.json</code></div>

          <pre id="hooks-win-claude-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-win-claude-${projectId}" style="font-size:10px;padding:4px 10px;margin-bottom:10px">Copy</button>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:0 0 4px">Codex - <code>~/.codex/config.toml</code></div>

          <pre id="hooks-win-codex-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-win-codex-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </details>

      <details style="border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">macOS / Linux manual config</summary>

        <div style="padding:0 10px 10px">

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:8px 0 4px">Claude Code - <code>~/.claude/settings.json</code></div>

          <pre id="hooks-unix-claude-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-unix-claude-${projectId}" style="font-size:10px;padding:4px 10px;margin-bottom:10px">Copy</button>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:0 0 4px">Codex - <code>~/.codex/config.toml</code></div>

          <pre id="hooks-unix-codex-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-unix-codex-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </details>

    </div>

  </details>`;


  }



  // ── PROJECT SETTINGS group ──────────────────────────────────────────────
  {
    let _psOpen = true;
    try { _psOpen = localStorage.getItem('meridian.settings.ps.' + projectId) !== '0'; } catch(e) {}
    const _psRot = _psOpen ? 'transform:rotate(90deg)' : '';
    html += `<details id="settings-grp-ps-${projectId}" ${_psOpen ? 'open' : ''} style="margin-bottom:12px;border:2px solid var(--border);border-radius:8px">` +
      `<summary style="cursor:pointer;list-style:none;padding:10px 14px;display:flex;align-items:center;gap:8px;background:var(--surface-2);border-radius:8px">` +
      `<span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;${_psRot}">▶</span>` +
      `<span style="font-weight:700;font-size:11px;color:var(--text);letter-spacing:.04em">PROJECT SETTINGS</span>` +
      `</summary><div style="padding:8px 8px 4px">`;
  }

  // "Connect claude.ai browser" card — always shown regardless of hosted/self-hosted
  html += _secHtml('connect', 'Connect Claude Code');

  const browserConnectorAccountNote = isHostedMode() ? `

    <div style="margin-top:6px;font-size:10px;color:var(--muted)">

      The browser connector uses whichever Meridian account is logged in at usemeridian.us in this

      browser tab. To use a different account, sign out and sign back in before reconnecting.

      <a href="/auth/logout?next=/auth/login" style="color:var(--accent);text-decoration:none;white-space:nowrap">Switch Meridian account →</a>

    </div>` : '';

  html += `<div style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">

      <div>

        <div style="font-weight:600;font-size:11px;color:var(--text);margin-bottom:2px">Browser connector</div>

        <div style="font-size:10px;color:var(--muted)">Use Meridian directly in Claude or ChatGPT - hosted MCP, no extension required</div>

      </div>

      <a href="https://docs.usemeridian.us/browser-connector/" target="_blank" style="white-space:nowrap;padding:4px 10px;background:var(--accent);color:#fff;border-radius:4px;font-size:10px;font-weight:600;text-decoration:none">Setup guide →</a>

    </div>

    ${browserConnectorAccountNote}

  </div>`;



  if (mcpData) {

  html += `<div style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)" id="github-card-${projectId}">

    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;flex-wrap:wrap">

      <div style="display:flex;align-items:center;gap:8px;min-width:0">

        ${githubIconSvg(14, 'var(--text)')}

        <div style="min-width:0">

          <div style="font-weight:600;font-size:11px;color:var(--text)">Connect GitHub repo</div>

          <div style="font-size:10px;color:var(--muted)">Connect your account and pick the repo your sessions should read from.</div>

        </div>

      </div>

      ${ghData?.connected ? `

        <div style="display:flex;align-items:center;gap:6px">

          <button class="secondary" id="github-test-btn-${projectId}" style="padding:3px 8px;font-size:10px">Test</button>

          <button class="secondary" id="github-disconnect-btn-${projectId}" style="padding:3px 8px;font-size:10px;color:var(--danger,#ef4444)">Disconnect</button>

          <span id="github-status-${projectId}" style="font-size:10px;color:var(--muted)"></span>

        </div>

      ` : ''}

    </div>

    ${ghData?.connected ? `

      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">

        <img src="${escapeHtml(ghAvatarUrl || 'https://github.com/github.png?size=48')}" alt="" style="width:26px;height:26px;border-radius:50%;object-fit:cover;border:1px solid var(--border);background:var(--surface-1)">

        <div style="min-width:0;flex:1">

          <div style="font-size:11px;font-weight:700;color:var(--text)">${ghUsername ? '@' + escapeHtml(ghUsername) : 'GitHub connected'}</div>

          <div style="font-size:9px;color:var(--muted)">${ghRepos.length ? `${ghRepos.length} accessible repos` : 'Fetching repo access…'}</div>

        </div>

      </div>

      <div style="display:grid;grid-template-columns:minmax(220px,1.4fr) minmax(110px,0.6fr) auto;gap:8px;align-items:end">

        <label style="display:flex;flex-direction:column;gap:3px;min-width:0">

          <span style="font-size:9px;color:var(--muted)">Repo</span>

          <select id="github-repo-${projectId}"

            style="padding:5px 8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">

            ${ghRepoOptions}

          </select>

          <span style="font-size:9px;color:var(--muted)">Grouped by owner so personal and org repos stay separate.</span>

        </label>

        <label style="display:flex;flex-direction:column;gap:3px;min-width:0">

          <span style="font-size:9px;color:var(--muted)">Branch</span>

          <select id="github-branch-${projectId}"

            style="padding:5px 8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">

            <option value="${escapeHtml(ghSelectedBranch)}" selected>${escapeHtml(ghSelectedBranch)}</option>

          </select>

        </label>

        <button class="primary" id="github-save-btn-${projectId}" style="padding:5px 12px;font-size:11px">Save repo</button>

      </div>

    ` : `

      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">

        <div style="font-size:10px;color:var(--muted)">Use GitHub OAuth to connect once. Meridian stores an encrypted token and pulls your repo list automatically.</div>

        <button class="primary" id="github-connect-btn-${projectId}" style="padding:5px 12px;font-size:11px">Connect with GitHub</button>

      </div>

    `}

  </div>`;

  }

  // ── Known Locations card — always visible ────────────────────────────────────
  {
    const _klCfg = (projectSettings && projectSettings.executor_config) || {};
    const _klPaths = Array.isArray(_klCfg.repo_paths) ? _klCfg.repo_paths : [];
    const _klHosts = Array.isArray(_klCfg.hostnames) ? _klCfg.hostnames : [];
    html += `<div style="margin-bottom:12px;padding:12px 14px;border:1px solid var(--border);border-radius:8px;background:var(--surface)">
      <div style="font-weight:600;font-size:13px;color:var(--text);margin-bottom:4px">Known Locations</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:10px">First hook from a new machine auto-registers it here. All future sessions from that machine route to this project regardless of directory.</div>
      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Registered Machines</div>
      <div id="exec-ez-hosts-tbl-${projectId}" style="margin-bottom:8px;font-size:10px;font-family:var(--font-mono)">${
        _klHosts.length
          ? '<table style="width:100%;border-collapse:collapse">' +
            _klHosts.map((h, i) => `<tr>
              <td style="padding:2px 6px 2px 0;color:var(--text)">${escapeHtml(h.hostname || '')}</td>
              <td style="padding:2px 6px 2px 0;color:var(--muted)"><label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:9px"><input type="checkbox" class="exec-ez-host-autocwd" data-pid="${escapeHtml(projectId)}" data-idx="${i}" ${h.auto_add_cwds ? 'checked' : ''} style="cursor:pointer"> Auto-add new cwds</label></td>
              <td style="padding:2px 0;text-align:right"><button class="exec-ez-del-host" data-pid="${escapeHtml(projectId)}" data-idx="${i}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">Remove</button></td>
            </tr>`).join('') + '</table>'
          : '<div style="color:var(--muted);font-style:italic;font-size:10px">No machines registered yet — first hook auto-registers.</div>'
      }</div>
      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Specific Paths (cwd overrides)</div>
      <div id="exec-ez-paths-tbl-${projectId}" style="margin-bottom:8px;font-size:10px;font-family:var(--font-mono)">${
        _klPaths.length
          ? '<table style="width:100%;border-collapse:collapse">' +
            _klPaths.map((p, i) => `<tr>
              <td style="padding:2px 6px 2px 0;color:var(--text)">${escapeHtml(p.hostname || '')}</td>
              <td style="padding:2px 6px 2px 0;color:var(--muted)">${escapeHtml(p.cwd || '')}</td>
              <td style="padding:2px 0;text-align:right"><button class="exec-ez-del-row" data-pid="${escapeHtml(projectId)}" data-idx="${i}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">Remove</button></td>
            </tr>`).join('') + '</table>'
          : '<div style="color:var(--muted);font-style:italic;font-size:10px">No path overrides — machine-level routing handles most cases.</div>'
      }</div>
      <div style="display:flex;gap:8px;align-items:center">
        <button id="exec-ez-save-${projectId}" class="primary" style="font-size:10px;padding:3px 10px">Save</button>
        <button id="exec-ez-clear-${projectId}" class="secondary" style="font-size:10px;padding:3px 10px">Clear all</button>
        <span id="exec-ez-status-${projectId}" style="font-size:10px;color:var(--muted);min-height:14px"></span>
      </div>
    </div>`;
  }



  if (!isHostedMode()) html += `<details class="meridian-disclosure" style="margin-bottom:16px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

    <summary style="cursor:pointer;list-style:none;padding:10px 12px;display:flex;justify-content:space-between;align-items:center;gap:8px">

      <span style="display:flex;align-items:center;gap:8px;flex:1;min-width:0">

        <span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;flex-shrink:0">▶</span>

        <span style="font-weight:600;font-size:11px;color:var(--text)">Meridian Connect</span>

      </span>

      <span style="font-size:10px;color:var(--muted);flex-shrink:0">Claude Code + Codex</span>

    </summary>

    <div style="padding:0 12px 12px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:10px">Install once per machine. Hooks auto-start sessions and sync context with Meridian.</div>

      ${osExecutorHintBanner(projectId)}

      <div style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));margin-bottom:10px">

        <div>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">macOS / Linux / WSL</div>

          <pre id="hooks-install-unix-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-install-unix-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

        <div>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Windows PowerShell</div>

          <pre id="hooks-install-windows-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-install-windows-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </div>

      <div style="font-size:10px;color:var(--muted);margin-top:6px">Need manual config? See <a href="https://docs.usemeridian.us/configuration" target="_blank" style="color:var(--accent);text-decoration:none">docs.usemeridian.us/configuration</a></div>

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;margin-top:10px">

        ${mcpData ? `<button id="hooks-gen-token-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Generate API key</button>` : ''}

        ${mcpData ? `<button id="hooks-gen-readonly-token-${projectId}" class="secondary" style="font-size:10px;padding:4px 10px" title="Read-only tokens can only call get_* tools — safe for ChatGPT connectors">Generate read-only key</button>` : ''}

        <span id="hooks-token-status-${projectId}" style="font-size:10px;color:var(--muted)">${mcpData ? 'Generate an API key to replace the placeholder token in the hosted snippets below.' : 'Local mode - no Bearer token needed.'}</span>

      </div>

      <div id="hooks-key-reveal-${projectId}" style="display:none;margin-bottom:8px;padding:8px 10px;border:1px solid var(--accent);border-radius:4px;background:var(--surface-1)">
        <div style="font-size:10px;color:var(--accent);font-weight:600;margin-bottom:6px">Your new API key — save it now, it won't be shown again:</div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <input id="hooks-key-reveal-input-${projectId}" type="text" readonly style="flex:1;min-width:180px;background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px">
          <button id="hooks-key-copy-btn-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Copy key</button>
          <button id="hooks-key-dismiss-${projectId}" class="secondary" style="font-size:10px;padding:4px 8px" title="Dismiss">×</button>
        </div>
      </div>

      ${mcpData ? `<div style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">

          <span style="font-size:10px;font-weight:600;color:var(--text)">Existing API keys</span>

          <button id="hooks-refresh-tokens-${projectId}" class="secondary" style="font-size:10px;padding:3px 8px">Refresh</button>

        </div>

        <div id="hooks-token-list-${projectId}" style="display:grid;gap:6px"></div>

      </div>` : ''}

      <details style="margin-bottom:10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">Windows manual config</summary>

        <div style="padding:0 10px 10px">

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:8px 0 4px">Claude Code - <code>~/.claude/settings.json</code></div>

          <pre id="hooks-win-claude-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-win-claude-${projectId}" style="font-size:10px;padding:4px 10px;margin-bottom:10px">Copy</button>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:0 0 4px">Codex - <code>~/.codex/config.toml</code></div>

          <pre id="hooks-win-codex-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-win-codex-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </details>

      <details style="border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">macOS / Linux manual config</summary>

        <div style="padding:0 10px 10px">

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:8px 0 4px">Claude Code - <code>~/.claude/settings.json</code></div>

          <pre id="hooks-unix-claude-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-unix-claude-${projectId}" style="font-size:10px;padding:4px 10px;margin-bottom:10px">Copy</button>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:0 0 4px">Codex - <code>~/.codex/config.toml</code></div>

          <pre id="hooks-unix-codex-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-unix-codex-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </details>

    </div>

  </details>`;



  // MCP config section (hosted mode only)

  if (false && mcpData) {

    const projects = mcpData.projects || [];

    const baseUrl = mcpData.base_url || 'https://usemeridian.us';

    const firstPid = projects[0]?.id || '';

    const clients = [

      { id: 'claude-desktop', label: 'Claude Desktop', file: '~/.config/Claude/claude_desktop_config.json' },

      { id: 'claude-code',    label: 'Claude Code',    file: '.mcp.json (project root)' },

      { id: 'cursor',         label: 'Cursor',         file: '~/.cursor/mcp.json' },

    ];

    const projectOpts = projects.map(p =>

      `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`

    ).join('');



    html += `<div style="margin-bottom:16px">

      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">

        <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase">MCP client setup</div>

        <a href="https://docs.usemeridian.us/browser-connector/" target="_blank" style="font-size:10px;color:var(--muted);text-decoration:none" title="Full setup guide">setup guide →</a>

      </div>

      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">

        <div style="display:flex;gap:0;border:1px solid var(--border);border-radius:3px;overflow:hidden" id="mcp-client-tabs-${projectId}">

          ${clients.map((c, i) => `<button data-client="${c.id}" style="background:${i===0?'var(--accent)':'var(--surface-1)'};color:${i===0?'#000':'var(--text)'};border:none;padding:3px 10px;font-size:10px;font-family:var(--font-mono);cursor:pointer;white-space:nowrap">${c.label}</button>`).join('')}

        </div>

        ${projects.length > 1 ? `<select id="mcp-project-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px">${projectOpts}</select>` : ''}

      </div>

      <pre id="mcp-config-block-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all">Generate an API key to see your config</pre>

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">

        <button id="mcp-gen-token-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Generate API key</button>

        <button id="mcp-copy-btn-${projectId}" class="secondary" style="font-size:10px;padding:4px 10px" disabled>Copy config</button>

        <span id="mcp-copy-status-${projectId}" style="font-size:10px;color:var(--muted)"></span>

      </div>

      <div id="mcp-key-reveal-${projectId}" style="display:none;margin-top:8px;padding:8px 10px;border:1px solid var(--accent);border-radius:4px;background:var(--surface-1)">
        <div style="font-size:10px;color:var(--accent);font-weight:600;margin-bottom:6px">Your new API key — save it now, it won't be shown again:</div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <input id="mcp-key-reveal-input-${projectId}" type="text" readonly style="flex:1;min-width:180px;background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px">
          <button id="mcp-key-copy-btn-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Copy key</button>
          <button id="mcp-key-dismiss-${projectId}" class="secondary" style="font-size:10px;padding:4px 8px" title="Dismiss">×</button>
        </div>
      </div>

      <div id="mcp-file-note-${projectId}" style="font-size:10px;color:var(--muted);margin-top:6px"></div>

    </div>`;



    // Wire after render

    setTimeout(() => {

      let activeClient = 'claude-desktop';

      let currentPid = firstPid;



      const configBlock = document.getElementById(`mcp-config-block-${projectId}`);

      const copyBtn = document.getElementById(`mcp-copy-btn-${projectId}`);

      const copyStatus = document.getElementById(`mcp-copy-status-${projectId}`);

      const fileNote = document.getElementById(`mcp-file-note-${projectId}`);

      const genBtn = document.getElementById(`mcp-gen-token-${projectId}`);

      const projectSel = document.getElementById(`mcp-project-${projectId}`);

      const tabsEl = document.getElementById(`mcp-client-tabs-${projectId}`);



      function buildConfig() {

        if (!currentToken) return null;

        return JSON.stringify({

          mcpServers: {

            meridian: {

              command: 'npx',

              args: ['-y', 'mcp-remote', `${baseUrl}/mcp`],

              env: { BEARER_TOKEN: currentToken },

            },

          },

        }, null, 2);

      }



      function renderConfig() {

        const cli = clients.find(c => c.id === activeClient) || clients[0];

        const cfg = buildConfig();

        if (cfg) {

          configBlock.textContent = cfg;

          copyBtn.disabled = false;

          fileNote.textContent = `Save to: ${cli.file}`;

        } else if (window.state.serverConfig?.demo_mode) {

          const demoKey = 'sk_meridian_demo_' + 'x'.repeat(24);

          const demoCfg = JSON.stringify({

            mcpServers: {

              meridian: {

                command: 'npx',

                args: ['-y', 'mcp-remote', `${baseUrl}/mcp`],

                env: { BEARER_TOKEN: demoKey },

              },

            },

          }, null, 2);

          configBlock.textContent = demoCfg;

          copyBtn.disabled = false;

          fileNote.textContent = `Demo key — sign up at ${baseUrl} for a real one`;

        } else {

          // Show placeholder config so the structure is immediately visible

          const placeholderKey = 'sk_meridian_' + 'x'.repeat(32);

          const placeholderCfg = JSON.stringify({

            mcpServers: {

              meridian: {

                command: 'npx',

                args: ['-y', 'mcp-remote', `${baseUrl}/mcp`],

                env: { BEARER_TOKEN: placeholderKey },

              },

            },

          }, null, 2);

          configBlock.textContent = placeholderCfg;

          copyBtn.disabled = false;

          fileNote.textContent = `Save to: ${cli.file}`;

          if (copyStatus) copyStatus.textContent = 'Click "Generate API key" to replace the placeholder with your real key.';

        }

      }



      if (tabsEl) {

        tabsEl.querySelectorAll('button[data-client]').forEach(btn => {

          btn.onclick = () => {

            activeClient = btn.dataset.client;

            tabsEl.querySelectorAll('button[data-client]').forEach(b => {

              b.style.background = b === btn ? 'var(--accent)' : 'var(--surface-1)';

              b.style.color = b === btn ? '#000' : 'var(--text)';

            });

            renderConfig();

          };

        });

      }



      if (projectSel) {

        projectSel.onchange = () => { currentPid = projectSel.value; renderConfig(); };

      }



      if (genBtn) {

        genBtn.onclick = async () => {

          genBtn.disabled = true;

          genBtn.textContent = 'Generating…';

          try {

            const tok = await api('/auth/tokens', { method: 'POST', body: JSON.stringify({ label: 'mcp-config' }) });

            currentToken = tok.token;

            renderConfig();

            // Also refresh the hosted .mcp.json box with the real token
            const pid = copyBtn?.id?.replace('mcp-copy-config-', '') || '';
            const hostedEl = document.getElementById(`hosted-mcp-json-${pid}`);
            if (hostedEl && currentToken) {
              const refreshedJson = JSON.stringify({mcpServers:{meridian:{command:"npx",args:["-y","mcp-remote","https://usemeridian.us/mcp"],env:{BEARER_TOKEN:currentToken}}}},null,2);
              hostedEl.textContent = refreshedJson;
              const copyHostedBtn = document.getElementById(`copy-hosted-mcp-json-${pid}`);
              if (copyHostedBtn) copyHostedBtn.onclick = async () => { try { await navigator.clipboard.writeText(refreshedJson); copyHostedBtn.textContent='Copied!'; setTimeout(()=>{copyHostedBtn.textContent='Copy .mcp.json';},1800); } catch(e){} };
            }

            copyBtn.disabled = false;

            // Show raw key reveal zone
            const mcpRevealEl = document.getElementById(`mcp-key-reveal-${projectId}`);
            const mcpRevealInput = document.getElementById(`mcp-key-reveal-input-${projectId}`);
            const mcpKeyCopyBtn = document.getElementById(`mcp-key-copy-btn-${projectId}`);
            const mcpKeyDismiss = document.getElementById(`mcp-key-dismiss-${projectId}`);
            if (mcpRevealEl && mcpRevealInput) {
              mcpRevealInput.value = tok.token;
              mcpRevealEl.style.display = '';
              function _hideMcpReveal() {
                mcpRevealEl.style.display = 'none';
                if (copyStatus) copyStatus.textContent = 'Key saved: ' + ('sk_meridian_••••••••' + tok.token.slice(-4)) + tok.token.slice(-4);
              }
              if (mcpKeyCopyBtn) mcpKeyCopyBtn.onclick = async () => { try { await navigator.clipboard.writeText(tok.token); mcpKeyCopyBtn.textContent = 'Copied!'; setTimeout(() => { mcpKeyCopyBtn.textContent = 'Copy key'; }, 1800); } catch(e) {} };
              if (mcpKeyDismiss) mcpKeyDismiss.onclick = _hideMcpReveal;
              setTimeout(_hideMcpReveal, 30000);
            } else if (copyStatus) {
              copyStatus.textContent = 'Real key generated — save it, it won\'t be shown again.';
            }

          } catch (e) {

            if (copyStatus) copyStatus.textContent = `error: ${escapeHtml(String(e))}`;

          } finally {

            genBtn.disabled = false;

            genBtn.textContent = 'Generate new key';

          }

        };

      }



      if (copyBtn) {

        copyBtn.onclick = async () => {

          const cfg = buildConfig();

          if (!cfg) return;

          try {

            await navigator.clipboard.writeText(cfg);

            copyBtn.textContent = 'Copied!';

            setTimeout(() => { copyBtn.textContent = 'Copy config'; }, 1800);

          } catch (e) {

            copyStatus.textContent = 'Copy failed — select and copy manually';

          }

        };

      }

    }, 0);

  }



  // Codex CLI section — always shown (works self-hosted via HTTP or STDIO)

  {

    const serverUrl = (window.state.serverConfig?.server_url || 'http://localhost:7878').replace(/\/$/, '');

    const mcpHttpUrl = `${serverUrl}/mcp`;

    const isHosted = window.location.hostname === 'usemeridian.us';

    const rawTomlPath = window.state.serverConfig?.toml_path || '';

    const cwd = rawTomlPath

      ? rawTomlPath.replace(/[/\\]meridian\.toml$/i, '').replace(/\\/g, '/')

      : (isHosted ? '' : '/path/to/your/meridian');

    const isDemo = !!window.state.serverConfig?.demo_mode;

    const displayPid = isDemo ? 'your-project-id' : projectId;



    const stdioText = `[mcp_servers.meridian]\ntype = "stdio"\ncommand = "pixi"\nargs = ["run", "python", "-m", "meridian", "--mcp"]\ncwd = "${cwd.replace(/"/g, '\\"')}"`;

    const httpText = `[mcp_servers.meridian]\ntype = "http"\nurl = "${mcpHttpUrl}"`;

    const _gCfg = (projectSettings && projectSettings.executor_config) || {};
    const _gAutoText = `/goal Complete pending sprint items in order. Done when all items\nmarked complete via complete_sprint_item(), ${_gCfg.test_cmd || 'pixi run test'} passes${_gCfg.test_min != null ? '\n' + _gCfg.test_min + '+,' : ','} generate_handoff() called. Stop after 40 turns or HITL.\n\nproject_id = "${displayPid}"`;
    const goalText = _gCfg.goal_template || _gAutoText;

    // Hosted .mcp.json for Claude Code
    const _mcpUrl = (window.state.serverConfig?.base_url || "https://usemeridian.us") + "/mcp";
    const hostedMcpJson = JSON.stringify({
      mcpServers: {
        meridian: {
          type: "http",
          url: _mcpUrl,
          headers: { Authorization: "Bearer sk_meridian_YOUR_KEY_HERE" }
        }
      }
    }, null, 2);



    // a7c43cc1 — claude --rc watcher installer collapsible
    html += `<details style="margin-top:12px;border:1px solid var(--border);border-radius:6px;overflow:hidden">
      <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text);background:var(--surface-2);list-style:none;display:flex;align-items:center;gap:6px;user-select:none">
        <span style="font-size:12px">⚡</span> Install rc watcher <span style="color:var(--muted);font-weight:400;margin-left:4px">(for <code>claude --rc</code> server mode)</span>
      </summary>
      <div style="padding:10px 12px;font-size:10px;color:var(--muted);line-height:1.8">
        <p style="margin:0 0 8px">When Claude runs in <code>claude --rc</code> (headless server mode) the
        standard SessionStart hooks do not fire. The rc watcher is a lightweight OS-native background service
        (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) that watches
        <code>~/.claude/projects/</code> for new session files and fires the hook automatically.</p>
        <div style="margin-bottom:6px;font-size:10px;color:var(--text);font-weight:600">Windows</div>
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:10px">
          <code id="rc-watcher-win-cmd-${escapeHtml(projectId)}" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;word-break:break-all">irm ${escapeHtml(hooksBaseUrl)}/install_watcher.ps1 | iex</code>
          <button onclick="navigator.clipboard.writeText(document.getElementById('rc-watcher-win-cmd-${escapeHtml(projectId)}').textContent).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)}).catch(()=>{})" style="padding:3px 8px;font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;cursor:pointer;white-space:nowrap;color:var(--text)">Copy</button>
        </div>
        <div style="margin-bottom:6px;font-size:10px;color:var(--text);font-weight:600">macOS / Linux</div>
        <div style="display:flex;gap:6px;align-items:center">
          <code id="rc-watcher-unix-cmd-${escapeHtml(projectId)}" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;word-break:break-all">curl -fsSL ${escapeHtml(hooksBaseUrl)}/install_watcher.sh | bash</code>
          <button onclick="navigator.clipboard.writeText(document.getElementById('rc-watcher-unix-cmd-${escapeHtml(projectId)}').textContent).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)}).catch(()=>{})" style="padding:3px 8px;font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;cursor:pointer;white-space:nowrap;color:var(--text)">Copy</button>
        </div>
      </div>
    </details>`;

    html += '</div></details>';  // close Connect Claude Code section
    html += _secHtml('executor', 'Executor Setup');

    html += `<div style="margin-bottom:16px">

      <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Codex CLI setup</div>

      ${isHostedMode() ? '' : `
      <div style="font-size:10px;color:var(--muted);margin-bottom:10px">Add to <code>~/.codex/config.toml</code> — or run <code>codex mcp add meridian ${escapeHtml(mcpHttpUrl)}</code></div>

      ${!isHosted ? `<div style="margin-bottom:12px">
        <label style="font-size:10px;color:var(--muted)">Your Meridian path<br>
          <input type="text" id="meridian-path-${escapeHtml(projectId)}" placeholder="/path/to/Meridian" value="${escapeHtml(rawTomlPath ? cwd : '')}" style="width:100%;max-width:400px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 8px;margin-top:3px;box-sizing:border-box">
        </label>
        <div style="font-size:9px;color:var(--muted);margin-top:3px">Updates the STDIO cwd below in real time.</div>
      </div>` : ""}

      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Option A — STDIO (local, recommended)</div>

      <pre id="codex-stdio-${escapeHtml(projectId)}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

      <button class="secondary" id="codex-copy-stdio-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px;margin-bottom:12px">Copy</button>

      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px;margin-top:8px">Option B — HTTP (when Meridian server is running)</div>

      <pre id="codex-http-${escapeHtml(projectId)}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

      <button class="secondary" id="codex-copy-http-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px;margin-bottom:12px">Copy</button>
      `}

      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px;margin-top:8px">/goal template</div>

      <textarea id="codex-goal-${escapeHtml(projectId)}" rows="6" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);resize:vertical;margin:0 0 4px 0;white-space:pre;box-sizing:border-box"></textarea>

      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

        <button class="secondary" id="codex-copy-goal-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px">Copy</button>

        <button class="primary" id="codex-save-goal-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px">Save</button>

        <button class="secondary" id="codex-regen-goal-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px">Regenerate</button>

        <span id="codex-goal-status-${escapeHtml(projectId)}" style="font-size:10px;color:var(--muted)"></span>

      </div>

      ${isHostedMode() ? `<div style="margin-top:12px;font-size:10px;color:var(--muted)">Need manual config? See <a href="https://docs.usemeridian.us/configuration" target="_blank" style="color:var(--accent);text-decoration:none">docs.usemeridian.us/configuration</a></div>

      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;overflow:hidden">
        <summary style="cursor:pointer;list-style:none;padding:6px 10px;background:var(--surface-2);font-size:10px;color:var(--muted)">Advanced — HTTP config (Codex / custom)</summary>
        <div style="padding:10px 12px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px">Add to <code>~/.codex/config.toml</code> — or run <code>codex mcp add meridian ${escapeHtml(mcpHttpUrl)}</code></div>
          <pre id="codex-http-${escapeHtml(projectId)}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>
          <button class="secondary" id="codex-copy-http-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px">Copy</button>
        </div>
      </details>` : ''}

    </div>`;



    setTimeout(() => {

      const stdioEl = document.getElementById(`codex-stdio-${projectId}`);

      const httpEl = document.getElementById(`codex-http-${projectId}`);

      const goalEl = document.getElementById(`codex-goal-${projectId}`);

      if (stdioEl) stdioEl.textContent = stdioText;

      if (httpEl) httpEl.textContent = httpText;

      if (goalEl) goalEl.value = goalText;

      function _codexCopySetup(btnId, text) {

        const btn = document.getElementById(btnId);

        if (!btn) return;

        btn.onclick = async () => {

          try {

            await navigator.clipboard.writeText(text);

            btn.textContent = 'Copied!';

            setTimeout(() => { btn.textContent = 'Copy'; }, 1800);

          } catch(e) { btn.textContent = 'Select and copy manually'; }

        };

      }

      _codexCopySetup(`codex-copy-stdio-${projectId}`, stdioText);

      _codexCopySetup(`codex-copy-http-${projectId}`, httpText);

      // Goal copy reads live textarea value
      const copyGoalBtn = document.getElementById(`codex-copy-goal-${projectId}`);
      if (copyGoalBtn && goalEl) {
        copyGoalBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(goalEl.value);
            copyGoalBtn.textContent = 'Copied!';
            setTimeout(() => { copyGoalBtn.textContent = 'Copy'; }, 1800);
          } catch(e) { copyGoalBtn.textContent = 'Select and copy manually'; }
        };
      }

      const saveGoalBtn = document.getElementById(`codex-save-goal-${projectId}`);
      const goalStatusEl = document.getElementById(`codex-goal-status-${projectId}`);
      if (saveGoalBtn && goalEl) {
        saveGoalBtn.onclick = async () => {
          saveGoalBtn.disabled = true;
          try {
            const curCfg = (projectSettings && projectSettings.executor_config) || {};
            await saveProjectSettings(projectId, { executor_config: { ...curCfg, goal_template: goalEl.value } });
            if (goalStatusEl) { goalStatusEl.textContent = 'Saved.'; setTimeout(() => { if (goalStatusEl) goalStatusEl.textContent = ''; }, 2000); }
          } catch(e) {
            if (goalStatusEl) goalStatusEl.textContent = `Failed: ${String(e)}`;
          } finally {
            saveGoalBtn.disabled = false;
          }
        };
      }

      const regenGoalBtn = document.getElementById(`codex-regen-goal-${projectId}`);
      if (regenGoalBtn && goalEl) {
        regenGoalBtn.onclick = async () => {
          goalEl.value = _gAutoText;
          regenGoalBtn.disabled = true;
          try {
            const curCfg = (projectSettings && projectSettings.executor_config) || {};
            const { goal_template: _gt, ...restCfg } = curCfg;
            await saveProjectSettings(projectId, { executor_config: restCfg });
            if (goalStatusEl) { goalStatusEl.textContent = 'Regenerated.'; setTimeout(() => { if (goalStatusEl) goalStatusEl.textContent = ''; }, 2000); }
          } catch(e) {
            if (goalStatusEl) goalStatusEl.textContent = `Failed: ${String(e)}`;
          } finally {
            regenGoalBtn.disabled = false;
          }
        };
      }

      const pushBtn = document.getElementById(`push-mcp-template-${projectId}`);
      if (pushBtn) {
        pushBtn.onclick = async () => {
          pushBtn.disabled = true;
          pushBtn.textContent = 'Pushing…';
          try {
            await api(`/projects/${projectId}/github/push-mcp-template`, { method: 'POST' });
            pushBtn.textContent = '✓ Pushed!';
            pushBtn.style.color = '#059669';
          } catch(e) {
            const msg = String(e);
            if (msg.includes('409')) {
              pushBtn.textContent = 'Already exists';
            } else {
              pushBtn.textContent = 'Failed: ' + msg.slice(0,40);
            }
            pushBtn.disabled = false;
          }
        };
      }

      const hostedMcpEl = document.getElementById(`hosted-mcp-json-${projectId}`);
      if (hostedMcpEl) hostedMcpEl.textContent = hostedMcpJson;
      _codexCopySetup(`copy-hosted-mcp-json-${projectId}`, hostedMcpJson);

      // Path input — live-update STDIO cwd
      const pathInput = document.getElementById(`meridian-path-${projectId}`);
      if (pathInput) {
        pathInput.addEventListener('input', function() {
          const newCwd = pathInput.value.trim() || '/path/to/your/meridian';
          const newStdio = '[mcp_servers.meridian]\ntype = "stdio"\ncommand = "pixi"\nargs = ["run", "python", "-m", "meridian", "--mcp"]\ncwd = "' + newCwd.replace(/"/g, '\\"') + '"';
          if (stdioEl) stdioEl.textContent = newStdio;
          _codexCopySetup(`codex-copy-stdio-${projectId}`, newStdio);
        });
      }

    }, 0);

  }



  html += '</div></details>';  // close Executor Setup section
  html += _secHtml('config', 'Project Config');

  html += `<div style="margin-bottom:16px">

    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Constitution</div>

    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">

      <label style="font-size:10px;color:var(--muted)">

        Max pinned decisions warning threshold<br>

        <input type="number" id="constitution-max-${projectId}" min="1" max="500" step="1" inputmode="numeric" style="margin-top:4px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;width:80px">

      </label>

      <span id="constitution-max-status-${projectId}" style="font-size:10px;color:var(--muted)">Warning updates the Decisions tab banner and archive suggestion.</span>

    </div>

  </div>`;



  setTimeout(() => {

    const sel = document.getElementById(`constitution-max-${projectId}`);

    const status = document.getElementById(`constitution-max-status-${projectId}`);

    if (!sel) return;

    sel.value = String(projectSettings.max_pinned_decisions || DEFAULT_MAX_PINNED_DECISIONS);

    const commit = async () => {

      // G1.6 — validate: integer, clamp to [1, 500]. Empty / NaN falls

      // back to the default so the user can never persist a garbage value.

      const raw = parseInt(String(sel.value || ''), 10);

      const nextLimit = Number.isFinite(raw)

        ? Math.min(500, Math.max(1, raw))

        : DEFAULT_MAX_PINNED_DECISIONS;

      sel.value = String(nextLimit);

      sel.disabled = true;

      try {

        const saved = await saveProjectSettings(projectId, { max_pinned_decisions: nextLimit });

        sel.value = String(saved.max_pinned_decisions || DEFAULT_MAX_PINNED_DECISIONS);

        if (status) status.textContent = `Warning threshold saved at ${saved.max_pinned_decisions}.`;

        renderConstitutionWarning(projectId);

      } catch (e) {

        if (status) status.textContent = `Save failed: ${String(e)}`;

      } finally {

        sel.disabled = false;

      }

    };

    // Save on blur and on Enter — match the calm-typing UX of free-text

    // fields elsewhere (saving on every keystroke would thrash the server).

    sel.addEventListener('change', commit);

    sel.addEventListener('blur', commit);

    sel.addEventListener('keydown', (e) => {

      if (e.key === 'Enter') { e.preventDefault(); sel.blur(); }

    });

  }, 0);



  // Human-in-the-loop section — per-project auto-answer toggle (v3.4)

  const hitlAuto = !!(projectSettings && projectSettings.hitl_auto_answer);

  html += `<div style="margin-bottom:16px">

    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Human-in-the-loop</div>

    <label style="display:flex;gap:8px;align-items:flex-start;font-size:11px;color:var(--text);cursor:pointer">

      <input type="checkbox" id="hitl-auto-${projectId}" ${hitlAuto ? 'checked' : ''} style="margin-top:2px">

      <span>Auto-answer HITL requests<br>

        <span style="font-size:9px;color:var(--muted)">When on, Meridian picks the first option automatically — no interruption. Review auto-answered requests in the Queue tab.</span>

      </span>

    </label>

    <div id="hitl-auto-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:4px;min-height:13px"></div>

  </div>`;



  setTimeout(() => {

    const cb = document.getElementById(`hitl-auto-${projectId}`);

    const status = document.getElementById(`hitl-auto-status-${projectId}`);

    if (!cb) return;

    cb.onchange = async () => {

      cb.disabled = true;

      try {

        const saved = await saveProjectSettings(projectId, { hitl_auto_answer: cb.checked });

        cb.checked = !!saved.hitl_auto_answer;

        if (status) status.textContent = saved.hitl_auto_answer

          ? 'Auto-answer ON — new requests resolve immediately.'

          : 'Auto-answer OFF — requests wait for a human.';

      } catch (e) {

        cb.checked = !cb.checked;

        if (status) status.textContent = `Save failed: ${String(e)}`;

      } finally {

        cb.disabled = false;

      }

    };

  }, 0);



  // Executor Config section

  const execCfg = (projectSettings && projectSettings.executor_config) || {};

  html += `<div style="margin-bottom:16px" id="executor-config-section-${projectId}">

    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Executor Config</div>

    <div style="font-size:10px;color:var(--muted);margin-bottom:8px">Per-project defaults injected into executor sessions via <code>start_session(role="executor")</code>. Set once; all executors inherit automatically.</div>

    <div style="margin-bottom:10px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:4px">repo_paths<br><span style="font-size:9px;color:var(--muted)">Auto-tracked by hooks. Delete rows to remove locations.</span></div>

      <div id="exec-repo-paths-tbl-${projectId}" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px">
        ${(() => {
          const rps = Array.isArray(execCfg.repo_paths) ? execCfg.repo_paths : [];
          if (!rps.length) return '<div style="color:var(--muted);font-style:italic">No locations tracked yet.</div>';
          return '<table style="width:100%;border-collapse:collapse">' +
            rps.map((p, i) => `<tr>
              <td style="padding:2px 6px 2px 0;color:var(--text)">${escapeHtml(p.hostname || '')}</td>
              <td style="padding:2px 6px 2px 0;color:var(--muted)">${escapeHtml(p.cwd || '')}</td>
              <td style="padding:2px 0;text-align:right"><button class="exec-del-rp-row" data-pid="${escapeHtml(projectId)}" data-idx="${i}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">✕</button></td>
            </tr>`).join('') + '</table>';
        })()}
      </div>

      <button id="exec-clear-paths-${projectId}" class="secondary" style="font-size:9px;padding:2px 8px">Clear all</button>

    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 12px">

      <label style="font-size:10px;color:var(--muted)">env_file<br><input id="exec-env_file-${projectId}" type="text" placeholder=".env file path" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.env_file || ''))}"></label>

      <label style="font-size:10px;color:var(--muted)">test_cmd<br><input id="exec-test_cmd-${projectId}" type="text" placeholder="pixi run test" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.test_cmd || ''))}"></label>

      <label style="font-size:10px;color:var(--muted)">test_min<br><input id="exec-test_min-${projectId}" type="number" placeholder="Min passing tests" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.test_min != null ? execCfg.test_min : ''))}"></label>

      <label style="font-size:10px;color:var(--muted)">deploy_cmd<br><input id="exec-deploy_cmd-${projectId}" type="text" placeholder="git push / fly deploy" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.deploy_cmd || ''))}"></label>

      <label style="font-size:10px;color:var(--muted)">branch<br><input id="exec-branch-${projectId}" type="text" placeholder="dev" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.branch || ''))}"></label>

    </div>

    <label style="display:block;font-size:10px;color:var(--muted);margin-top:10px">

      Checkpoint after <span id="exec-context_threshold-val-${projectId}" style="color:var(--text);font-family:var(--font-mono)">${escapeHtml(String(execCfg.context_threshold || DEFAULT_CONTEXT_THRESHOLD))}</span> turns

      <input id="exec-context_threshold-${projectId}" type="range" min="10" max="100" step="5" value="${escapeHtml(String(execCfg.context_threshold || DEFAULT_CONTEXT_THRESHOLD))}" style="width:100%;max-width:320px;margin-top:4px;display:block">

      <span style="font-size:9px;color:var(--muted)">When a session passes this many turns, <code>get_context_block</code> nudges it to checkpoint.</span>

    </label>

    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">

      <button id="exec-save-${projectId}" class="primary" style="font-size:10px;padding:3px 10px">Save</button>

      <span id="exec-status-${projectId}" style="font-size:10px;color:var(--muted);min-height:14px"></span>

    </div>

  </div>`;



  setTimeout(() => {

    const saveBtn = document.getElementById(`exec-save-${projectId}`);

    const statusEl = document.getElementById(`exec-status-${projectId}`);

    if (!saveBtn) return;

    // Live-update the slider's value label as the user drags.

    const ctxSlider = document.getElementById(`exec-context_threshold-${projectId}`);

    const ctxVal = document.getElementById(`exec-context_threshold-val-${projectId}`);

    if (ctxSlider && ctxVal) {

      ctxSlider.addEventListener('input', () => { ctxVal.textContent = ctxSlider.value; });

    }

    // Wire repo_paths delete/clear in Executor Config section
    let _execRepoPaths = Array.isArray(execCfg.repo_paths) ? [...execCfg.repo_paths] : [];
    const _rpTblEl = document.getElementById(`exec-repo-paths-tbl-${projectId}`);
    const _rerenderRpTbl = () => {
      if (!_rpTblEl) return;
      if (!_execRepoPaths.length) {
        _rpTblEl.innerHTML = '<div style="color:var(--muted);font-style:italic;font-size:10px">No locations tracked yet.</div>';
      } else {
        _rpTblEl.innerHTML = '<table style="width:100%;border-collapse:collapse">' +
          _execRepoPaths.map((p, i) => `<tr>
            <td style="padding:2px 6px 2px 0;color:var(--text);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p.hostname||'')}</td>
            <td style="padding:2px 6px 2px 0;color:var(--muted);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p.cwd||'')}</td>
            <td style="padding:2px 0;text-align:right"><button class="exec-del-rp-row" data-pid="${escapeHtml(projectId)}" data-idx="${i}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">✕</button></td>
          </tr>`).join('') + '</table>';
        _rpTblEl.querySelectorAll('.exec-del-rp-row').forEach(b => {
          b.onclick = () => { _execRepoPaths.splice(parseInt(b.dataset.idx, 10), 1); _rerenderRpTbl(); };
        });
      }
    };
    if (_rpTblEl) {
      _rpTblEl.querySelectorAll('.exec-del-rp-row').forEach(b => {
        b.onclick = () => { _execRepoPaths.splice(parseInt(b.dataset.idx, 10), 1); _rerenderRpTbl(); };
      });
    }
    const _clearRpBtn = document.getElementById(`exec-clear-paths-${projectId}`);
    if (_clearRpBtn) { _clearRpBtn.onclick = () => { _execRepoPaths = []; _rerenderRpTbl(); }; }

    saveBtn.onclick = async () => {

      saveBtn.disabled = true;

      const fields = ['env_file', 'test_cmd', 'deploy_cmd', 'branch'];

      const cfg = {};

      cfg.repo_paths = _execRepoPaths;
      if (Array.isArray(execCfg.hostnames)) cfg.hostnames = execCfg.hostnames;

      for (const f of fields) {

        const val = (document.getElementById(`exec-${f}-${projectId}`)?.value || '').trim();

        if (val) cfg[f] = val;

      }

      const minEl = document.getElementById(`exec-test_min-${projectId}`);

      const minVal = minEl ? parseInt(minEl.value || '', 10) : NaN;

      if (!isNaN(minVal) && minVal > 0) cfg.test_min = minVal;

      const ctxRaw = ctxSlider ? parseInt(ctxSlider.value || '', 10) : NaN;

      if (!isNaN(ctxRaw)) cfg.context_threshold = Math.min(100, Math.max(10, ctxRaw));

      try {

        await saveProjectSettings(projectId, { executor_config: cfg });

        if (statusEl) statusEl.textContent = 'Saved.';

        setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2000);

      } catch (e) {

        if (statusEl) statusEl.textContent = `Save failed: ${String(e)}`;

      } finally {

        saveBtn.disabled = false;

      }

    };

  }, 0);



  // Workspace section — moved to ACCOUNT & WORKSPACE group below.



  setTimeout(() => {

    // --- Settings defaults ---

    const hitlCb = document.getElementById('ws-hitl-default');

    const sprintIn = document.getElementById('ws-sprint-default');

    const displayIn = document.getElementById('ws-display-name');

    const nudgeIn = document.getElementById('ws-nudge-threshold');

    const handoffTplIn = document.getElementById('ws-handoff-template');

    const saveBtn = document.getElementById('ws-settings-save');

    const saveStatus = document.getElementById('ws-settings-status');

    (async () => {

      try {

        const s = await api('/workspace/settings');

        if (hitlCb) hitlCb.checked = !!s.hitl_auto_answer_default;

        if (sprintIn) sprintIn.value = s.sprint_name_default || '';

        if (displayIn) displayIn.value = s.display_name || '';

        if (nudgeIn) nudgeIn.value = s.log_task_sprint_nudge_threshold != null ? s.log_task_sprint_nudge_threshold : 5;

        if (handoffTplIn) handoffTplIn.value = s.handoff_template || '';

      } catch (e) { /* defaults shown */ }

    })();

    if (saveBtn) saveBtn.onclick = async () => {

      saveBtn.disabled = true;

      try {

        const nudgeVal = nudgeIn ? parseInt(nudgeIn.value, 10) : 5;

        await api('/workspace/settings', {

          method: 'PATCH',

          body: JSON.stringify({

            hitl_auto_answer_default: !!(hitlCb && hitlCb.checked),

            sprint_name_default: (sprintIn && sprintIn.value.trim()) || '',

            display_name: (displayIn && displayIn.value.trim()) || '',

            log_task_sprint_nudge_threshold: isNaN(nudgeVal) ? 5 : Math.max(0, nudgeVal),

            handoff_template: (handoffTplIn && handoffTplIn.value.trim()) || '',

          }),

        });

        if (saveStatus) saveStatus.textContent = 'Saved.';

        setTimeout(() => { if (saveStatus) saveStatus.textContent = ''; }, 2000);

      } catch (e) {

        if (saveStatus) saveStatus.textContent = `Save failed: ${String(e)}`;

      } finally {

        saveBtn.disabled = false;

      }

    };



    // --- Decisions ---

    const decList = document.getElementById('ws-decisions-list');

    async function renderWsDecisions() {

      if (!decList) return;

      try {

        const items = await api('/workspace/decisions');

        decList.innerHTML = (items && items.length)

          ? items.map(d => `<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">

              <span><span style="color:var(--accent)">${escapeHtml(d.category || 'TECHNICAL')}</span> ${escapeHtml(d.title || '')}: <span style="color:var(--muted)">${escapeHtml(d.body || '')}</span></span>

              <button class="secondary" data-did="${escapeHtml(d.id)}" style="font-size:9px;padding:2px 7px">×</button>

            </div>`).join('')

          : '<div style="color:var(--muted)">No workspace decisions yet.</div>';

        decList.querySelectorAll('button[data-did]').forEach(btn => {

          btn.onclick = async () => {

            if (!confirm('Delete this workspace decision?')) return;

            try { await api(`/workspace/decisions/${btn.dataset.did}`, { method: 'DELETE' }); renderWsDecisions(); }

            catch (e) { alert('Error: ' + e); }

          };

        });

      } catch (e) { decList.innerHTML = '<div style="color:var(--muted)">Failed to load.</div>'; }

    }

    renderWsDecisions();

    const decAdd = document.getElementById('ws-dec-add');

    if (decAdd) decAdd.onclick = async () => {

      const title = (document.getElementById('ws-dec-title')?.value || '').trim();

      const body = (document.getElementById('ws-dec-body')?.value || '').trim();

      if (!title || !body) return;

      decAdd.disabled = true;

      try {

        await api('/workspace/decisions', { method: 'POST', body: JSON.stringify({ title, body }) });

        document.getElementById('ws-dec-title').value = '';

        document.getElementById('ws-dec-body').value = '';

        renderWsDecisions();

      } catch (e) { alert('Error: ' + e); } finally { decAdd.disabled = false; }

    };



    // --- Notes ---

    const noteList = document.getElementById('ws-notes-list');

    async function renderWsNotes() {

      if (!noteList) return;

      try {

        const items = await api('/workspace/notes');

        noteList.innerHTML = (items && items.length)

          ? items.map(n => `<div data-note-row="${escapeHtml(n.id)}" style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">

              <span data-note-view="${escapeHtml(n.id)}">${escapeHtml(n.title || '')}: <span style="color:var(--muted)">${escapeHtml(n.body || '')}</span>${n.tags ? ` <span style="color:var(--accent);font-size:9px">${escapeHtml(n.tags)}</span>` : ''}</span>

              <span style="display:flex;gap:4px;flex-shrink:0">
                <button class="secondary" data-nid-edit="${escapeHtml(n.id)}" data-ntitle="${escapeHtml(n.title || '')}" data-nbody="${escapeHtml(n.body || '')}" style="font-size:9px;padding:2px 6px" title="Edit">✎</button>
                <button class="secondary" data-nid-move="${escapeHtml(n.id)}" style="font-size:9px;padding:2px 6px" title="Move to project">↗</button>
                <button class="secondary" data-nid="${escapeHtml(n.id)}" style="font-size:9px;padding:2px 7px">×</button>
              </span>

            </div>`).join('')

          : '<div style="color:var(--muted)">No workspace notes yet.</div>';

        noteList.querySelectorAll('button[data-nid]').forEach(btn => {

          btn.onclick = async () => {

            if (!confirm('Delete this workspace note?')) return;

            try { await api(`/workspace/notes/${btn.dataset.nid}`, { method: 'DELETE' }); renderWsNotes(); }

            catch (e) { alert('Error: ' + e); }

          };

        });

        noteList.querySelectorAll('button[data-nid-edit]').forEach(btn => {

          btn.onclick = () => {

            const nid = btn.dataset.nidEdit;
            const row = noteList.querySelector(`[data-note-row="${nid}"]`);
            const view = noteList.querySelector(`[data-note-view="${nid}"]`);
            if (!row || row.querySelector('textarea')) return;
            const titleVal = btn.dataset.ntitle;
            const bodyVal = btn.dataset.nbody;
            view.style.display = 'none';
            const edit = document.createElement('div');
            edit.style.cssText = 'flex:1;display:flex;flex-direction:column;gap:4px';
            edit.innerHTML = `
              <input type="text" value="${escapeHtml(titleVal)}" style="font-size:10px;padding:2px 6px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:3px;width:100%">
              <textarea rows="2" style="font-size:10px;padding:2px 6px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:3px;resize:vertical;width:100%">${escapeHtml(bodyVal)}</textarea>
              <span style="display:flex;gap:4px">
                <button class="primary" style="font-size:9px;padding:2px 8px">Save</button>
                <button class="secondary" style="font-size:9px;padding:2px 8px">Cancel</button>
              </span>`;
            row.insertBefore(edit, row.querySelector('[data-note-view]').nextSibling);
            edit.querySelector('button.secondary').onclick = () => { edit.remove(); view.style.display = ''; };
            edit.querySelector('button.primary').onclick = async () => {
              const newTitle = edit.querySelector('input').value.trim();
              const newBody = edit.querySelector('textarea').value.trim();
              if (!newTitle || !newBody) return;
              try {
                await api(`/workspace/notes/${nid}`, { method: 'PATCH', body: JSON.stringify({ title: newTitle, body: newBody }) });
                renderWsNotes();
              } catch (e) { alert('Error: ' + e); }
            };

          };

        });

        noteList.querySelectorAll('button[data-nid-move]').forEach(btn => {

          btn.onclick = () => {

            const nid = btn.dataset.nidMove;
            const row = noteList.querySelector(`[data-note-row="${nid}"]`);
            if (!row || row.querySelector('select[data-move-select]')) return;
            const projects = window.state.projects || [];
            if (!projects.length) { alert('No projects to move to.'); return; }
            const picker = document.createElement('span');
            picker.style.cssText = 'display:flex;gap:4px;align-items:center;flex-shrink:0';
            picker.innerHTML = `
              <select data-move-select style="font-size:9px;padding:2px 4px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:3px;max-width:120px">
                ${projects.map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)}</option>`).join('')}
              </select>
              <button class="primary" data-move-go style="font-size:9px;padding:2px 8px">Move</button>
              <button class="secondary" data-move-cancel style="font-size:9px;padding:2px 6px">×</button>`;
            const actions = btn.parentElement;
            actions.style.display = 'none';
            row.appendChild(picker);
            picker.querySelector('[data-move-cancel]').onclick = () => { picker.remove(); actions.style.display = ''; };
            picker.querySelector('[data-move-go]').onclick = async () => {
              const targetId = picker.querySelector('[data-move-select]').value;
              if (!targetId) return;
              try {
                await api(`/workspace/notes/${nid}/move`, { method: 'POST', body: JSON.stringify({ project_id: targetId }) });
                renderWsNotes();
                // Refresh the target project's notes tab if it happens to be open.
                try { if (typeof loadNotesTab === 'function') await loadNotesTab(targetId); } catch (_) { /* tab not open */ }
              } catch (e) { alert('Error: ' + e); }
            };

          };

        });

      } catch (e) { noteList.innerHTML = '<div style="color:var(--muted)">Failed to load.</div>'; }

    }

    renderWsNotes();

    const noteAdd = document.getElementById('ws-note-add');

    if (noteAdd) noteAdd.onclick = async () => {

      const title = (document.getElementById('ws-note-title')?.value || '').trim();

      const body = (document.getElementById('ws-note-body')?.value || '').trim();

      if (!title || !body) return;

      noteAdd.disabled = true;

      try {

        await api('/workspace/notes', { method: 'POST', body: JSON.stringify({ title, body }) });

        document.getElementById('ws-note-title').value = '';

        document.getElementById('ws-note-body').value = '';

        renderWsNotes();

      } catch (e) { alert('Error: ' + e); } finally { noteAdd.disabled = false; }

    };

  }, 0);



  html += '</div></details>';  // close Project Config section
  html += '</div></details>';  // close PROJECT SETTINGS group

  // ── ACCOUNT & WORKSPACE group ────────────────────────────────────────────
  {
    let _awOpen = false;
    try { _awOpen = localStorage.getItem('meridian.settings.aw.' + projectId) === '1'; } catch(e) {}
    const _awRot = _awOpen ? 'transform:rotate(90deg)' : '';
    html += `<details id="settings-grp-aw-${projectId}" ${_awOpen ? 'open' : ''} style="margin-bottom:12px;border:2px solid var(--border);border-radius:8px">` +
      `<summary style="cursor:pointer;list-style:none;padding:10px 14px;display:flex;align-items:center;gap:8px;background:var(--surface-2);border-radius:8px">` +
      `<span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;${_awRot}">▶</span>` +
      `<span style="font-weight:700;font-size:11px;color:var(--text);letter-spacing:.04em">ACCOUNT &amp; WORKSPACE</span>` +
      `</summary><div style="padding:8px 8px 4px">`;
  }

  html += _secHtml('account', 'Account');

  html += `<div style="margin-bottom:16px" id="workspace-section-${projectId}">
    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Workspace</div>
    <div style="font-size:10px;color:var(--muted);margin-bottom:10px">Applies across <strong>all projects</strong> in this workspace. Notes and decisions here are injected at the top of every project's context block.</div>
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--text);margin-bottom:4px">Default settings</div>
      <label style="display:flex;gap:8px;align-items:flex-start;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:6px">
        <input type="checkbox" id="ws-hitl-default" style="margin-top:2px">
        <span>Auto-answer HITL by default<br>
          <span style="font-size:9px;color:var(--muted)">Suggested default for new projects' HITL auto-answer toggle.</span>
        </span>
      </label>
      <label style="font-size:10px;color:var(--muted);display:block">Default sprint name<br>
        <input id="ws-sprint-default" type="text" placeholder="e.g. june-sprint" style="width:100%;max-width:240px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px">
      </label>
      <label style="font-size:10px;color:var(--muted);display:block;margin-top:6px">Your display name<br>
        <input id="ws-display-name" type="text" placeholder="e.g. Adam" style="width:100%;max-width:240px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px">
        <span style="display:block;font-size:9px;color:var(--muted);margin-top:2px">Used to attribute Claude/Codex hook sessions to you on the activity timeline when they don't set a name.</span>
      </label>
      <label style="font-size:10px;color:var(--muted);display:block;margin-top:6px">log_task nudge threshold (0 = off)<br>
        <input id="ws-nudge-threshold" type="number" min="0" max="100" placeholder="5" style="width:80px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px">
        <span style="display:block;font-size:9px;color:var(--muted);margin-top:2px">After this many inline log_task calls with no sprint items, show a nudge to file sprint items. Default: 5.</span>
      </label>
      <div style="font-size:10px;color:var(--text);margin:12px 0 4px">Handoff Format</div>
      <label style="font-size:10px;color:var(--muted);display:block">Custom full-mode handoff template (leave blank for default)<br>
        <textarea id="ws-handoff-template" rows="6" placeholder="# Handoff&#10;Sprint: {{sprint}}&#10;&#10;## Recent Tasks&#10;{{recent_tasks}}&#10;&#10;## Pending&#10;{{pending_items}}" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:6px 8px;margin-top:2px;resize:vertical"></textarea>
        <span style="display:block;font-size:9px;color:var(--muted);margin-top:2px">Placeholders: {{sprint}}, {{recent_tasks}}, {{decisions}}, {{north_star}}, {{version_goal}}, {{pending_items}}, {{notes}}. Blank = default handoff.</span>
      </label>
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
        <button id="ws-settings-save" class="primary" style="font-size:10px;padding:3px 10px">Save defaults</button>
        <span id="ws-settings-status" style="font-size:10px;color:var(--muted);min-height:14px"></span>
      </div>
    </div>
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--text);margin-bottom:4px">Workspace decisions</div>
      <div id="ws-decisions-list" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px"><div style="color:var(--muted)">loading…</div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <input id="ws-dec-title" type="text" placeholder="Title" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:1;min-width:120px">
        <input id="ws-dec-body" type="text" placeholder="Body" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:2;min-width:160px">
        <button id="ws-dec-add" class="primary" style="font-size:10px;padding:4px 10px">Pin</button>
      </div>
    </div>
    <div>
      <div style="font-size:10px;color:var(--text);margin-bottom:4px">Workspace notes</div>
      <div id="ws-notes-list" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px"><div style="color:var(--muted)">loading…</div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <input id="ws-note-title" type="text" placeholder="Title" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:1;min-width:120px">
        <input id="ws-note-body" type="text" placeholder="Body" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:2;min-width:160px">
        <button id="ws-note-add" class="primary" style="font-size:10px;padding:4px 10px">Add</button>
      </div>
    </div>
  </div>`;

  // Local file reading snippet — self-hosted only, only when repo_paths are set
  if (!isHostedMode() && _allRepoPaths.length > 0) {
    const _fsPaths = _allRepoPaths.map(p => JSON.stringify(p)).join(' ');
    const _fsNpx = `npx -y @modelcontextprotocol/server-filesystem ${_allRepoPaths.map(p => JSON.stringify(p)).join(' ')}`;
    const _fsClaude = `claude mcp add filesystem -- npx -y @modelcontextprotocol/server-filesystem ${_allRepoPaths.map(p => JSON.stringify(p)).join(' ')}`;
    html += `<div style="margin-bottom:16px" id="fs-mcp-section-${projectId}">
      <details>
        <summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;padding-bottom:6px;border-bottom:1px solid var(--border);margin-bottom:8px">
          <span style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase">Local file reading for planning chat</span>
          <span style="font-size:9px;color:var(--muted);margin-left:auto">▼</span>
        </summary>
        <div style="font-size:10px;color:var(--muted);margin-bottom:8px">Add a filesystem MCP server so Claude can read your repo files during planning conversations.</div>
        <div style="margin-bottom:8px">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px">npx command:</div>
          <div style="display:flex;gap:6px;align-items:flex-start">
            <code id="fs-mcp-npx-${projectId}" style="flex:1;display:block;padding:6px 8px;border:1px solid var(--border);border-radius:3px;background:var(--surface-1);color:var(--text);font-size:9px;font-family:var(--font-mono);white-space:pre-wrap;word-break:break-all">${escapeHtml(_fsNpx)}</code>
            <button class="secondary" style="font-size:9px;padding:3px 8px;flex-shrink:0" onclick="navigator.clipboard.writeText(${JSON.stringify(_fsNpx)}).then(()=>toast('Copied')).catch(()=>toast('Copy failed',true))">Copy</button>
          </div>
        </div>
        <div style="margin-bottom:8px">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px">claude mcp add (Claude Code):</div>
          <div style="display:flex;gap:6px;align-items:flex-start">
            <code style="flex:1;display:block;padding:6px 8px;border:1px solid var(--border);border-radius:3px;background:var(--surface-1);color:var(--text);font-size:9px;font-family:var(--font-mono);white-space:pre-wrap;word-break:break-all">${escapeHtml(_fsClaude)}</code>
            <button class="secondary" style="font-size:9px;padding:3px 8px;flex-shrink:0" onclick="navigator.clipboard.writeText(${JSON.stringify(_fsClaude)}).then(()=>toast('Copied')).catch(()=>toast('Copy failed',true))">Copy</button>
          </div>
        </div>
        <div style="font-size:9px;color:var(--muted);line-height:1.5">Requires Node.js. Add the generated URL as a second connector in claude.ai.<br>WSL users: localhost works directly. Remote/SSH: use <code style="font-size:8px">cloudflared tunnel --url http://localhost:PORT</code></div>
      </details>
    </div>`;
  }

  // Team members section (hosted mode only) — always visible for all plan tiers (ecdae392)

  if (isHostedMode()) {

    html += `<div style="margin-bottom:16px" id="members-section-${projectId}">

      <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Team members</div>

      <div id="members-list-${projectId}" style="margin-bottom:10px;font-size:11px;font-family:var(--font-mono)"><div style="color:var(--muted)">loading…</div></div>

      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

        <input id="invite-email-${projectId}" type="email" placeholder="teammate@example.com" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:1;min-width:160px">

        <select id="invite-role-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 6px">

          <option value="admin">admin</option>

          <option value="member" selected>member</option>

          <option value="viewer">viewer</option>

        </select>

        <button id="invite-btn-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Invite</button>

      </div>

      <div id="invite-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:5px;min-height:14px"></div>

    </div>`;



    setTimeout(async () => {

      const listEl = document.getElementById(`members-list-${projectId}`);

      const inviteBtn = document.getElementById(`invite-btn-${projectId}`);

      const inviteEmail = document.getElementById(`invite-email-${projectId}`);

      const inviteRole = document.getElementById(`invite-role-${projectId}`);

      const inviteStatus = document.getElementById(`invite-status-${projectId}`);



      async function renderMembers() {

        if (!listEl) return;

        try {

          const members = await api('/workspace/members');

          if (!members || members.length === 0) {

            listEl.innerHTML = '<div style="color:var(--muted);font-size:10px">No team members yet.</div>';

            return;

          }

          const ROLE_CHOICES = ['admin', 'member', 'viewer'];

          listEl.innerHTML = members.map(m => {

            const opts = ROLE_CHOICES.map(r => `<option value="${r}" ${m.role === r ? 'selected' : ''}>${r}</option>`).join('');

            return `

            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">

              <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(m.email)}${m.pending ? ' <span style="color:var(--accent);font-size:9px;font-weight:600">Invited</span>' : ''}</span>

              ${m.pending ? `<button class="resend-invite-btn secondary" data-mid="${escapeHtml(m.id)}" title="Resend invite" style="font-size:9px;padding:2px 7px">Resend</button>` : `<select class="member-role-select" data-mid="${escapeHtml(m.id)}" title="Change role" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 5px">${opts}</select>`}

              <button class="secondary" data-mid="${escapeHtml(m.id)}" title="Remove member" style="font-size:9px;padding:2px 7px">×</button>

            </div>`;

          }).join('');

          listEl.querySelectorAll('select.member-role-select').forEach(sel => {

            sel.dataset.prev = sel.value;

            sel.onchange = async () => {

              const newRole = sel.value;

              sel.disabled = true;

              try {

                await api(`/workspace/members/${sel.dataset.mid}`, {

                  method: 'PATCH', body: JSON.stringify({ role: newRole }),

                });

                sel.dataset.prev = newRole;

                if (inviteStatus) { inviteStatus.textContent = `Role updated to ${newRole}.`; setTimeout(() => { if (inviteStatus) inviteStatus.textContent = ''; }, 2500); }

              } catch (e) {

                sel.value = sel.dataset.prev;  // revert when the server rejects (e.g. 403)

                if (inviteStatus) inviteStatus.textContent = `Error: ${escapeHtml(String(e))}`;

              } finally {

                sel.disabled = false;

              }

            };

          });

          listEl.querySelectorAll('button.resend-invite-btn').forEach(btn => {

            btn.onclick = async () => {

              btn.disabled = true;

              btn.textContent = '…';

              try {

                await api(`/workspace/invite/${btn.dataset.mid}/resend`, { method: 'POST' });

                btn.textContent = 'Sent';

                if (inviteStatus) { inviteStatus.textContent = 'Invite resent.'; setTimeout(() => { if (inviteStatus) inviteStatus.textContent = ''; }, 2500); }

              } catch(e) {

                btn.textContent = 'Resend';

                if (inviteStatus) inviteStatus.textContent = `Error: ${escapeHtml(String(e))}`;

              } finally {

                btn.disabled = false;

              }

            };

          });

          listEl.querySelectorAll('button[data-mid]:not(.resend-invite-btn)').forEach(btn => {

            btn.onclick = async () => {

              if (!confirm('Remove this member?')) return;

              try {

                await api(`/workspace/members/${btn.dataset.mid}`, { method: 'DELETE' });

                renderMembers();

              } catch(e) { alert('Error: ' + e); }

            };

          });

        } catch(e) {

          if (listEl) listEl.innerHTML = '<div style="color:var(--muted);font-size:10px">Members only available in hosted mode.</div>';

        }

      }

      renderMembers();



      if (inviteBtn) {

        inviteBtn.onclick = async () => {

          const email = (inviteEmail?.value || '').trim();

          const role = inviteRole?.value || 'member';

          if (!email) { if (inviteStatus) inviteStatus.textContent = 'Enter an email address.'; return; }

          inviteBtn.disabled = true;

          inviteBtn.textContent = 'Sending…';

          try {

            await api('/workspace/invite', { method: 'POST', body: JSON.stringify({ email, role }) });

            if (inviteEmail) inviteEmail.value = '';

            if (inviteStatus) { inviteStatus.textContent = `Invite sent to ${email}.`; setTimeout(() => { if (inviteStatus) inviteStatus.textContent = ''; }, 3000); }

            renderMembers();

          } catch(e) {

            if (inviteStatus) inviteStatus.textContent = `Error: ${escapeHtml(String(e))}`;

          } finally {

            inviteBtn.disabled = false;

            inviteBtn.textContent = 'Invite';

          }

        };

      }

    }, 0);

  }



  // Account section (hosted mode only; hidden in demo mode)

  const isDemo = !!window.state.serverConfig?.demo_mode;

  if (mcpData && !isDemo) {

    html += `<div style="margin-bottom:16px">

      <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Account</div>

      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">

        <a href="/export/my-data" download style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 10px;text-decoration:none;cursor:pointer">Export my data</a>

        <span style="font-size:10px;color:var(--muted)">Download a JSON file of all your account data (GDPR).</span>

      </div>

      <div style="border:1px solid #7f1d1d;border-radius:4px;padding:10px;background:#1a0a0a">

        <div style="color:#f87171;font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px">Danger zone</div>

        <div style="font-size:10px;color:#9ca3af;margin-bottom:8px">Permanently delete your account, cancel your subscription, and erase all data. Cannot be undone.</div>

        <button id="delete-account-btn-${projectId}" style="background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b;border-radius:3px;padding:4px 12px;font-size:10px;font-family:var(--font-mono);cursor:pointer">Delete my account</button>

        <div id="delete-account-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:5px;min-height:14px"></div>

      </div>

    </div>`;



    setTimeout(() => {

      const deleteBtn = document.getElementById(`delete-account-btn-${projectId}`);

      const deleteStatus = document.getElementById(`delete-account-status-${projectId}`);

      if (!deleteBtn) return;

      deleteBtn.onclick = async () => {

        const typed = prompt('Export your data first. Then type DELETE to permanently delete your account:');

        if (typed !== 'DELETE') return;

        if (!confirm('Final confirmation: your Stripe subscription will be cancelled and all data erased. Continue?')) return;

        deleteBtn.disabled = true;

        deleteBtn.textContent = 'Deleting…';

        try {

          await api('/account/delete', { method: 'POST', body: JSON.stringify({ confirmation: 'DELETE' }) });

          window.location.href = '/';

        } catch(e) {

          if (deleteStatus) deleteStatus.textContent = `Error: ${escapeHtml(String(e))}`;

          deleteBtn.disabled = false;

          deleteBtn.textContent = 'Delete my account';

        }

      };

    }, 0);

  }



  // Usage section (hosted mode only)

  if (mcpData) {

    html += `<div style="margin-bottom:16px" id="usage-section-${projectId}">

      <div id="usage-header-${projectId}" style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Usage this month</div>

      <div id="usage-body-${projectId}" style="font-size:10px;color:var(--muted)">loading…</div>

    </div>`;



    setTimeout(async () => {

      const usageEl = document.getElementById(`usage-body-${projectId}`);

      if (!usageEl) return;

      try {

        const u = await api('/settings/usage');

        const c = u.compute || {};

        const s = u.storage || {};

        const unlimited = !!u.unlimited;



        // Header reflects the plan: free tier is a one-time 30-day trial, not monthly.

        const headerEl = document.getElementById(`usage-header-${projectId}`);

        if (headerEl) {

          if (u.plan === 'free') headerEl.textContent = 'Trial usage';

          else if (unlimited) headerEl.textContent = 'Usage';

          else headerEl.textContent = 'Usage this month';

        }



        function pct(used, limit) { return Math.min(100, limit > 0 ? (used / limit * 100) : 0); }

        function barColor(p) { return p >= 100 ? '#ef4444' : p >= 80 ? '#f59e0b' : 'var(--accent)'; }



        if (unlimited) {

          usageEl.innerHTML = `

            <div style="margin-bottom:10px;display:flex;justify-content:space-between">

              <span style="color:var(--text)">Compute</span>

              <span>${c.used.toFixed(2)} CU-hrs <span style="color:var(--accent)">· Unlimited</span></span>

            </div>

            <div style="margin-bottom:4px;display:flex;justify-content:space-between">

              <span style="color:var(--text)">Storage</span>

              <span>${s.used_gb.toFixed(3)} GB <span style="color:var(--accent)">· Unlimited</span></span>

            </div>`;

          return;

        }



        const cpct = pct(c.used, c.grace);

        const spct = pct(s.used_gb, s.limit_gb);



        usageEl.innerHTML = `

          <div style="margin-bottom:10px">

            <div style="display:flex;justify-content:space-between;margin-bottom:3px">

              <span style="color:var(--text)">Compute${c.throttled ? ' <span style="color:#ef4444">(throttled)</span>' : ''}</span>

              <span>${c.used.toFixed(2)} / ${c.limit} CU-hrs <span style="color:var(--muted)">(${c.grace} w/grace)</span></span>

            </div>

            <div style="background:var(--surface-1);border-radius:2px;height:5px;overflow:hidden">

              <div style="background:${barColor(cpct)};width:${cpct}%;height:100%;transition:width .3s"></div>

            </div>

          </div>

          <div style="margin-bottom:12px">

            <div style="display:flex;justify-content:space-between;margin-bottom:3px">

              <span style="color:var(--text)">Storage</span>

              <span>${s.used_gb.toFixed(3)} / ${s.limit_gb} GB</span>

            </div>

            <div style="background:var(--surface-1);border-radius:2px;height:5px;overflow:hidden">

              <div style="background:${barColor(spct)};width:${spct}%;height:100%;transition:width .3s"></div>

            </div>

          </div>

          <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">

            <label style="font-size:10px;color:var(--muted)">

              Compute overage budget ($USD/mo, 0 = throttle)<br>

              <input id="compute-cap-${projectId}" type="number" min="0" step="1" value="${c.cap_usd}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;width:80px;margin-top:3px">

            </label>

            <label style="font-size:10px;color:var(--muted)">

              Storage overage budget ($USD/mo, 0 = block)<br>

              <input id="storage-cap-${projectId}" type="number" min="0" step="1" value="${s.cap_usd}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;width:80px;margin-top:3px">

            </label>

            <button id="save-caps-${projectId}" class="secondary" style="font-size:10px;padding:4px 10px;align-self:flex-end">Save</button>

            <span id="caps-status-${projectId}" style="font-size:10px;color:var(--muted);min-height:14px;align-self:flex-end"></span>

          </div>`;



        const saveBtn = document.getElementById(`save-caps-${projectId}`);

        const capsStatus = document.getElementById(`caps-status-${projectId}`);

        if (saveBtn) {

          saveBtn.onclick = async () => {

            const cc = parseFloat(document.getElementById(`compute-cap-${projectId}`)?.value || '0');

            const sc = parseFloat(document.getElementById(`storage-cap-${projectId}`)?.value || '0');

            saveBtn.disabled = true;

            try {

              await api('/settings/usage', { method: 'PATCH', body: JSON.stringify({ compute_cap: cc, storage_cap: sc }) });

              if (capsStatus) { capsStatus.textContent = 'saved'; setTimeout(() => { capsStatus.textContent = ''; }, 2000); }

            } catch(e) {

              if (capsStatus) capsStatus.textContent = `error: ${escapeHtml(String(e))}`;

            } finally { saveBtn.disabled = false; }

          };

        }

      } catch(e) {

        if (usageEl) usageEl.textContent = 'Usage data unavailable.';

      }

    }, 0);

  }



  // Notifications card — generalised: ntfy, webhook, or email

  const ntfyData = (ntfyResult.status === 'fulfilled') ? ntfyResult.value : null;

  // prefer notify_url key, fall back to ntfy_url for older servers

  const savedNotifyUrl = ntfyData ? (ntfyData.notify_url || ntfyData.ntfy_url || '') : '';

  const savedNotifyEmail = ntfyData ? (ntfyData.notify_email || '') : '';

  // targets show topic-only (the https://ntfy.sh/ prefix is implied).

  const defaultNotifyUrl = displayNotifyTarget(savedNotifyUrl);

  // ntfy security warning: shown once until user acknowledges via localStorage.

  let ntfyWarnAcknowledged = false;

  try { ntfyWarnAcknowledged = localStorage.getItem(STORAGE_KEY('ntfy.warn.dismissed')) === '1'; } catch(e) {}

  const ntfyInputDisabled = ntfyWarnAcknowledged ? '' : 'disabled';

  const ntfyWarnDisplay = ntfyWarnAcknowledged ? 'display:none' : '';

  html += `<div data-demo-hide style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

    <div style="font-weight:600;font-size:11px;color:var(--text);margin-bottom:4px">Notifications</div>

    <div style="font-size:10px;color:var(--muted);margin-bottom:8px">

      Save a push/webhook target and an email target independently.

      Alerts fire on HITL requests and sprint completions. No account needed for ntfy.

    </div>

    <div id="ntfy-warn-${projectId}" style="margin-bottom:8px;padding:8px 10px;border:1px solid #f59e0b88;border-radius:5px;background:#f59e0b11;font-size:10px;color:#f59e0b;line-height:1.5;${ntfyWarnDisplay}">

      <strong>⚠ Security notice:</strong> ntfy.sh topics are public — anyone who knows your topic name can subscribe and read your alerts. Use a long, random topic name (e.g. <code>my-project-a7f3k2</code>) or self-host ntfy for privacy. Slack/Discord webhooks and email are private alternatives.<br>

      <label style="display:flex;align-items:center;gap:6px;margin-top:6px;cursor:pointer;color:var(--text)">

        <input type="checkbox" id="ntfy-warn-ack-${projectId}" style="cursor:pointer;accent-color:#f59e0b">

        I understand my ntfy topic is public

      </label>

    </div>

    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

      <label style="font-size:10px;color:var(--muted);white-space:nowrap;min-width:100px">ntfy_url:</label>

      <input type="text" id="ntfy-url-${projectId}"

        value="${escapeHtml(defaultNotifyUrl)}"

        placeholder="${escapeHtml(suggestNtfyTopic(projectId))}  ·  https://hooks.slack.com/…"

        ${ntfyInputDisabled}

        style="flex:1;min-width:200px;padding:5px 8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;opacity:${ntfyWarnAcknowledged ? '1' : '0.4'}">

      <button class="secondary" id="ntfy-save-${projectId}" ${ntfyInputDisabled} style="padding:4px 10px;font-size:10px;opacity:${ntfyWarnAcknowledged ? '1' : '0.4'}">Save</button>

      <button class="secondary" id="ntfy-test-${projectId}" ${ntfyInputDisabled} style="padding:4px 10px;font-size:10px;opacity:${ntfyWarnAcknowledged ? '1' : '0.4'}" title="Send a test notification to verify your URL">Test</button>

      <span id="ntfy-status-${projectId}" style="font-size:10px;color:var(--muted);min-width:40px"></span>

    </div>

    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px">

      <label style="font-size:10px;color:var(--muted);white-space:nowrap;min-width:100px">notify_email:</label>

      <input type="email" id="notify-email-${projectId}"
        value="${escapeHtml(savedNotifyEmail || window.state.tenantEmail || '')}"
        placeholder="you@example.com"
        style="flex:1;min-width:180px;padding:5px 8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">

      <button class="secondary" id="notify-email-save-${projectId}" style="padding:4px 10px;font-size:10px">Save</button>

      <span id="notify-email-status-${projectId}" style="font-size:10px;color:var(--muted);min-width:40px"></span>

    </div>

    <div style="font-size:9px;color:var(--muted);margin-top:4px;line-height:1.6">

      <strong>ntfy</strong> — install the ntfy app (iOS / Android / desktop), pick any topic name, and type it here. The <code>https://ntfy.sh/</code> prefix is added for you.<br>

      <strong>Email</strong> — save <code>notify_email</code> separately to get alerts by email (hosted only; fires independently from ntfy).<br>

      <strong>Webhook</strong> — paste any <code>https://</code> URL (Slack, Discord, or your own) to receive a JSON POST.

    </div>

  </div>`;



  // Notifications section

  if (prefs !== null) {

    html += `<div style="margin-bottom:12px">

      <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Email notifications</div>`;

    PREFS.forEach(p => {

      const checked = prefs[p.key] ? 'checked' : '';

      html += `<label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;cursor:pointer;font-family:var(--font-mono);font-size:11px;color:var(--text)">

        <input type="checkbox" data-pref="${p.key}" ${checked} style="cursor:pointer">

        ${escapeHtml(p.label)}

      </label>`;

    });

    html += `<div id="settings-save-status-${projectId}" style="font-size:10px;color:var(--muted);min-height:14px;margin-top:6px"></div>`;

    html += '</div>';

  } else if (!mcpData) {

    html += '<div style="color:var(--muted);font-size:11px;padding:8px 0">Settings are only available in hosted mode (usemeridian.us).</div>';

  }

  html += '</div></details>';  // close Account section
  html += '</div></details>';  // close ACCOUNT & WORKSPACE group

  try {
    body.innerHTML = html;
  } catch (renderErr) {
    console.error('Settings render failed:', renderErr);
    body.innerHTML = `<div style="color:var(--error);font-size:11px">Failed to render settings: ${escapeHtml(String(renderErr))}</div>`;
    return;
  }

  if (isDemoMode()) hideDemoAdminControls();

  // Settings section accordion — persist open/closed state per project
  setTimeout(() => {
    ['connect', 'executor', 'config', 'account'].forEach(function(k) {
      const det = document.getElementById('settings-sec-' + k + '-' + projectId);
      if (!det) return;
      det.addEventListener('toggle', function() {
        try {
          const ss = JSON.parse(localStorage.getItem('meridian.settings.sections.' + projectId) || '{}');
          ss[k] = det.open;
          localStorage.setItem('meridian.settings.sections.' + projectId, JSON.stringify(ss));
        } catch(e) {}
        const caret = det.querySelector(':scope > summary .meridian-caret');
        if (caret) caret.style.transform = det.open ? 'rotate(90deg)' : '';
      });
    });

    // Outer group toggles (PROJECT SETTINGS / ACCOUNT & WORKSPACE)
    const psGrp = document.getElementById('settings-grp-ps-' + projectId);
    if (psGrp) {
      psGrp.addEventListener('toggle', function() {
        try { localStorage.setItem('meridian.settings.ps.' + projectId, psGrp.open ? '1' : '0'); } catch(e) {}
        const c = psGrp.querySelector(':scope > summary .meridian-caret');
        if (c) c.style.transform = psGrp.open ? 'rotate(90deg)' : '';
      });
    }
    const awGrp = document.getElementById('settings-grp-aw-' + projectId);
    if (awGrp) {
      awGrp.addEventListener('toggle', function() {
        try { localStorage.setItem('meridian.settings.aw.' + projectId, awGrp.open ? '1' : '0'); } catch(e) {}
        const c = awGrp.querySelector(':scope > summary .meridian-caret');
        if (c) c.style.transform = awGrp.open ? 'rotate(90deg)' : '';
      });
    }
  }, 0);

  // Known Locations card — hostnames + repo_paths
  setTimeout(() => {
    const ezSaveBtn = document.getElementById(`exec-ez-save-${projectId}`);
    const ezClearBtn = document.getElementById(`exec-ez-clear-${projectId}`);
    const ezStatus = document.getElementById(`exec-ez-status-${projectId}`);
    if (!ezSaveBtn) return;

    const _execCfgBase = (projectSettings && projectSettings.executor_config) || {};
    let _ezHosts = Array.isArray(_execCfgBase.hostnames) ? [..._execCfgBase.hostnames] : [];
    let _ezPaths = Array.isArray(_execCfgBase.repo_paths) ? [..._execCfgBase.repo_paths] : [];

    const _rerenderHostsTbl = () => {
      const tbl = document.getElementById(`exec-ez-hosts-tbl-${projectId}`);
      if (!tbl) return;
      tbl.innerHTML = _ezHosts.length
        ? '<table style="width:100%;border-collapse:collapse">' +
          _ezHosts.map((h, i) => `<tr>
            <td style="padding:2px 6px 2px 0;color:var(--text);font-size:10px;font-family:var(--font-mono)">${escapeHtml(h.hostname || '')}</td>
            <td style="padding:2px 6px 2px 0;color:var(--muted)"><label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:9px"><input type="checkbox" class="exec-ez-host-autocwd" data-pid="${escapeHtml(projectId)}" data-idx="${i}" ${h.auto_add_cwds ? 'checked' : ''} style="cursor:pointer"> Auto-add new cwds</label></td>
            <td style="padding:2px 0;text-align:right"><button class="exec-ez-del-host" data-pid="${escapeHtml(projectId)}" data-idx="${i}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">✕</button></td>
          </tr>`).join('') + '</table>'
        : '<div style="color:var(--muted);font-style:italic;font-size:10px">No machines registered yet — first hook auto-registers.</div>';
      _wireHostBtns();
    };

    const _wireHostBtns = () => {
      document.querySelectorAll(`.exec-ez-del-host[data-pid="${projectId}"]`).forEach(btn => {
        btn.onclick = () => { _ezHosts.splice(parseInt(btn.dataset.idx, 10), 1); _rerenderHostsTbl(); };
      });
      document.querySelectorAll(`.exec-ez-host-autocwd[data-pid="${projectId}"]`).forEach(cb => {
        cb.onchange = () => {
          const idx = parseInt(cb.dataset.idx, 10);
          if (_ezHosts[idx]) _ezHosts[idx] = { ..._ezHosts[idx], auto_add_cwds: cb.checked };
        };
      });
    };
    _wireHostBtns();

    const _rerenderPathsTbl = () => {
      const tbl = document.getElementById(`exec-ez-paths-tbl-${projectId}`);
      if (!tbl) return;
      tbl.innerHTML = _ezPaths.length
        ? '<table style="width:100%;border-collapse:collapse">' +
          _ezPaths.map((p, i) => `<tr>
            <td style="padding:2px 6px 2px 0;color:var(--text);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p.hostname || '')}</td>
            <td style="padding:2px 6px 2px 0;color:var(--muted);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p.cwd || '')}</td>
            <td style="padding:2px 0;text-align:right"><button class="exec-ez-del-row" data-pid="${escapeHtml(projectId)}" data-idx="${i}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">✕</button></td>
          </tr>`).join('') + '</table>'
        : '<div style="color:var(--muted);font-style:italic;font-size:10px">No path overrides — machine-level routing handles most cases.</div>';
      _wirePathBtns();
    };

    const _wirePathBtns = () => {
      document.querySelectorAll(`.exec-ez-del-row[data-pid="${projectId}"]`).forEach(btn => {
        btn.onclick = () => { _ezPaths.splice(parseInt(btn.dataset.idx, 10), 1); _rerenderPathsTbl(); };
      });
    };
    _wirePathBtns();

    if (ezClearBtn) {
      ezClearBtn.onclick = () => {
        _ezHosts = []; _ezPaths = [];
        _rerenderHostsTbl(); _rerenderPathsTbl();
      };
    }

    ezSaveBtn.onclick = async () => {
      ezSaveBtn.disabled = true;
      const curCfg = (projectSettings && projectSettings.executor_config) || {};
      const cfg = { ...curCfg, hostnames: _ezHosts, repo_paths: _ezPaths };
      delete cfg.repo_path;
      try {
        await saveProjectSettings(projectId, { executor_config: cfg });
        if (ezStatus) { ezStatus.textContent = 'Saved.'; setTimeout(() => { if (ezStatus) ezStatus.textContent = ''; }, 2000); }
      } catch(e) {
        if (ezStatus) ezStatus.textContent = `Save failed: ${String(e)}`;
      } finally {
        ezSaveBtn.disabled = false;
      }
    };
  }, 0);




  // G4.18 — Delete account confirmation flow. Demands typing DELETE in a

  // prompt so a misclick can't nuke the tenant. Sends to /account/delete

  // (hosted-only endpoint); on success redirects to /.

  const deleteBtn = document.getElementById(`account-delete-${projectId}`);

  if (deleteBtn) {

    deleteBtn.onclick = async () => {

      const typed = window.prompt(

        "This permanently deletes your account, projects, and data. "

        + "Stripe subscription is canceled and the Neon DB is dropped.\n\n"

        + "Type DELETE to confirm.",

      );

      if (typed !== "DELETE") {

        if (typed !== null) toast('Account NOT deleted (confirmation did not match).');

        return;

      }

      deleteBtn.disabled = true;

      try {

        await api('/account/delete', {

          method: 'POST',

          body: JSON.stringify({ confirmation: 'DELETE' }),

        });

        toast('Account deleted. Signing out…');

        setTimeout(() => { window.location.href = '/'; }, 1200);

      } catch (e) {

        toast('Delete failed: ' + e.message, true);

        deleteBtn.disabled = false;

      }

    };

  }

  // e7d4400b — Billing portal: POST to avoid GET redirect leaking session cookie,
  // redirect client-side once we have the URL.
  const billingPortalBtn = document.getElementById(`billing-portal-btn-${projectId}`);

  if (billingPortalBtn) {

    billingPortalBtn.onclick = async () => {

      billingPortalBtn.disabled = true;

      billingPortalBtn.textContent = 'Loading…';

      try {

        const data = await api('/billing/portal', { method: 'POST' });

        window.location.href = data.url;

      } catch (e) {

        toast('Could not open billing portal: ' + e.message, true);

        billingPortalBtn.disabled = false;

        billingPortalBtn.textContent = 'Manage billing →';

      }

    };

  }


  // Shared token variable for both setTimeout blocks (MCP config and hooks config)
  var currentToken = null;

  setTimeout(() => {

    const hostedPlaceholderToken = mcpData ? ('sk_meridian_' + 'x'.repeat(32)) : '';

    let hooksToken = null;



    const renderHooks = () => {

      const activeToken = hooksToken || hostedPlaceholderToken;

      const installUnix = `curl -fsSL ${hooksBaseUrl}/install.sh | sh`;

      const installWindows = `irm ${hooksBaseUrl}/install.ps1 | iex`;

      const snippets = {

        [`hooks-install-unix-${projectId}`]: installUnix,

        [`hooks-install-windows-${projectId}`]: installWindows,

        [`hooks-win-claude-${projectId}`]: buildClaudeHookSnippet('windows', activeToken),

        [`hooks-win-codex-${projectId}`]: buildCodexHookSnippet('windows', activeToken),

        [`hooks-unix-claude-${projectId}`]: buildClaudeHookSnippet('unix', activeToken),

        [`hooks-unix-codex-${projectId}`]: buildCodexHookSnippet('unix', activeToken),

      };

      Object.entries(snippets).forEach(([id, text]) => {

        const el = document.getElementById(id);

        if (el) el.textContent = text;

      });

      const statusEl = document.getElementById(`hooks-token-status-${projectId}`);

      if (statusEl) {

        if (hooksToken) {

          statusEl.textContent = 'Real API key generated - hosted snippets are prefilled for this user and project.';

        } else if (mcpData) {

          statusEl.textContent = 'Generate an API key to replace the placeholder token in the hosted snippets below.';

        } else {

          statusEl.textContent = 'Local mode - no Bearer token needed.';

        }

      }

    };

    const tokenListEl = document.getElementById(`hooks-token-list-${projectId}`);

    const renderHooksTokenList = (tokens) => {

      if (!tokenListEl) return;

      if (!Array.isArray(tokens) || !tokens.length) {

        tokenListEl.innerHTML = `<div style="font-size:10px;color:var(--muted)">No API keys yet. Generate one above to prefill the hosted snippets.</div>`;

        return;

      }

      tokenListEl.innerHTML = tokens.map((token) => {

        const isReadOnly = (token.token_type || 'readwrite') === 'readonly';

        const typeBadge = `<span style="font-size:9px;padding:1px 5px;border-radius:3px;border:1px solid ${isReadOnly ? '#fbbf24' : 'var(--accent)'};color:${isReadOnly ? '#fbbf24' : 'var(--accent)'};margin-left:4px">${isReadOnly ? 'read-only' : 'read-write'}</span>`;

        return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2)">
          <div style="min-width:0;flex:1">
            <div style="font-size:10px;color:var(--text);font-family:var(--font-mono);word-break:break-all;display:flex;align-items:center;gap:4px">${escapeHtml(token.masked_token || 'sk_meridian_...')}${typeBadge}</div>
            <div style="font-size:9px;color:var(--muted);margin-top:2px">${escapeHtml(token.label || 'API key')} - ${escapeHtml(token.created_at || '')}</div>
          </div>
          <button class="secondary" data-token-id="${escapeHtml(token.id || '')}" data-token-label="${escapeHtml(token.label || '')}" style="font-size:10px;padding:3px 8px;color:var(--danger,#ef4444)">Revoke</button>
        </div>`;

      }).join('');

      tokenListEl.querySelectorAll('[data-token-id]').forEach((btn) => {

        btn.onclick = async () => {

          const tokenId = btn.getAttribute('data-token-id');

          if (!tokenId) return;

          const _revokeLabel = btn.getAttribute('data-token-label') || '';
          const _isHooksKey = _revokeLabel.includes('hooks') || _revokeLabel.includes('installer');
          const _revokeMsg = _isHooksKey
            ? 'This key may be used in your Claude Code hooks.\n\nAfter revoking, re-run: irm https://usemeridian.us/install.ps1 | iex\n\nRevoke anyway?'
            : 'Revoke this API key? Existing clients using it will stop working.';
          if (!confirm(_revokeMsg)) return;

          btn.disabled = true;

          try {

            await api(`/auth/tokens/${tokenId}`, { method: 'DELETE' });

            const statusEl = document.getElementById(`hooks-token-status-${projectId}`);

            if (statusEl) statusEl.textContent = 'API key revoked.';

            await loadHooksTokens();

          } catch (e) {

            btn.disabled = false;

            const statusEl = document.getElementById(`hooks-token-status-${projectId}`);

            if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e))}`;

          }

        };

      });

    };

    async function loadHooksTokens() {

      if (!tokenListEl) return;

      tokenListEl.innerHTML = `<div style="font-size:10px;color:var(--muted)">Loading API keys...</div>`;

      try {

        const tokens = await api('/auth/tokens');

        renderHooksTokenList(tokens);

      } catch (e) {

        tokenListEl.innerHTML = `<div style="font-size:10px;color:var(--danger,#ef4444)">Could not load API keys.</div>`;

        const statusEl = document.getElementById(`hooks-token-status-${projectId}`);

        if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e))}`;

      }

    }



    const wireCopy = (buttonId, targetId) => {

      const btn = document.getElementById(buttonId);

      const target = document.getElementById(targetId);

      if (!btn || !target) return;

      btn.onclick = async () => {

        try {

          await navigator.clipboard.writeText(target.textContent || '');

          btn.textContent = 'Copied!';

          setTimeout(() => { btn.textContent = 'Copy'; }, 1800);

        } catch (e) {

          btn.textContent = 'Select and copy manually';

        }

      };

    };



    [

      ['hooks-copy-install-unix-' + projectId, 'hooks-install-unix-' + projectId],

      ['hooks-copy-install-windows-' + projectId, 'hooks-install-windows-' + projectId],

      ['hooks-copy-win-claude-' + projectId, 'hooks-win-claude-' + projectId],

      ['hooks-copy-win-codex-' + projectId, 'hooks-win-codex-' + projectId],

      ['hooks-copy-unix-claude-' + projectId, 'hooks-unix-claude-' + projectId],

      ['hooks-copy-unix-codex-' + projectId, 'hooks-unix-codex-' + projectId],

    ].forEach(([buttonId, targetId]) => wireCopy(buttonId, targetId));



    function _showKeyReveal(rawToken, label) {
      const revealEl = document.getElementById(`hooks-key-reveal-${projectId}`);
      const revealInput = document.getElementById(`hooks-key-reveal-input-${projectId}`);
      const revealCopyBtn = document.getElementById(`hooks-key-copy-btn-${projectId}`);
      const revealDismiss = document.getElementById(`hooks-key-dismiss-${projectId}`);
      if (!revealEl || !revealInput) return;
      revealInput.value = rawToken;
      revealEl.style.display = 'block';
      function _hideReveal() {
        revealEl.style.display = 'none';
        const masked = 'sk_meridian_\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' + rawToken.slice(-4);
        const statusEl = document.getElementById(`hooks-token-status-${projectId}`);
        if (statusEl) statusEl.textContent = `${label || 'Key'} saved: ${masked} — use "Generate new key" to rotate.`;
      }
      if (revealCopyBtn) {
        revealCopyBtn.onclick = async () => {
          try { await navigator.clipboard.writeText(rawToken); revealCopyBtn.textContent = 'Copied!'; setTimeout(() => { revealCopyBtn.textContent = 'Copy key'; }, 1800); } catch(e) {}
        };
      }
      if (revealDismiss) revealDismiss.onclick = _hideReveal;
      setTimeout(_hideReveal, 30000);
    }

    const genBtn = document.getElementById(`hooks-gen-token-${projectId}`);

    if (genBtn) {

      genBtn.onclick = async () => {

        genBtn.disabled = true;

        genBtn.textContent = 'Generating...';

        try {

          const tok = await api('/auth/tokens', { method: 'POST', body: JSON.stringify({ label: 'hooks-config' }) });

          hooksToken = tok.token;
          currentToken = tok.token; // Share with browser connector section

          renderHooks();
          await loadHooksTokens();

          // Also update hosted .mcp.json box if present
          const hostedMcpEl2 = document.getElementById(`hosted-mcp-json-${projectId}`);
          if (hostedMcpEl2) {
            const j = JSON.stringify({mcpServers:{meridian:{command:"npx",args:["-y","mcp-remote","https://usemeridian.us/mcp"],env:{BEARER_TOKEN:tok.token}}}},null,2);
            hostedMcpEl2.textContent = j;
          }

          _showKeyReveal(tok.token, 'API key');

        } catch (e) {

          const statusEl = document.getElementById(`hooks-token-status-${projectId}`);

          if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e))}`;

        } finally {

          genBtn.disabled = false;

          genBtn.textContent = hooksToken ? 'Generate new key' : 'Generate API key';

        }

      };

    }

    const genReadonlyBtn = document.getElementById(`hooks-gen-readonly-token-${projectId}`);

    if (genReadonlyBtn) {

      genReadonlyBtn.onclick = async () => {

        genReadonlyBtn.disabled = true;

        genReadonlyBtn.textContent = 'Generating...';

        try {

          const tok = await api('/auth/tokens', { method: 'POST', body: JSON.stringify({ label: 'readonly', token_type: 'readonly' }) });

          _showKeyReveal(tok.token, 'Read-only key');

          await loadHooksTokens();

        } catch (e) {

          const statusEl = document.getElementById(`hooks-token-status-${projectId}`);

          if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e))}`;

        } finally {

          genReadonlyBtn.disabled = false;

          genReadonlyBtn.textContent = 'Generate read-only key';

        }

      };

    }

    const refreshBtn = document.getElementById(`hooks-refresh-tokens-${projectId}`);

    if (refreshBtn) {

      refreshBtn.onclick = async () => {

        refreshBtn.disabled = true;

        try {

          await loadHooksTokens();

        } finally {

          refreshBtn.disabled = false;

        }

      };

    }

    renderHooks();
    if (tokenListEl) loadHooksTokens();

  }, 0);



  // Wire notification Save button

  const ntfySaveBtn = document.getElementById(`ntfy-save-${projectId}`);

  if (ntfySaveBtn) {

    ntfySaveBtn.onclick = async () => {

      const inp = document.getElementById(`ntfy-url-${projectId}`);

      const statusEl = document.getElementById(`ntfy-status-${projectId}`);

      // G1.7 — send the raw user input; the server canonicalizes ntfy

      // entries to topic-only and suffixes for per-DB uniqueness. The

      // response carries the saved value, which we reflect back into

      // the field so the user sees what we actually stored.

      const raw = (inp ? inp.value : '').trim() || null;

      try {

        const saved = await api(`/projects/${projectId}/ntfy`, {

          method: 'PATCH',

          body: JSON.stringify({ notify_url: raw, ntfy_url: raw }),

        });

        const savedVal = saved && (saved.notify_url || saved.ntfy_url || '');

        const shownVal = displayNotifyTarget(savedVal || '');

        if (inp) inp.value = shownVal || '';

        if (statusEl) {

          statusEl.textContent = shownVal && raw && shownVal.toLowerCase() !== displayNotifyTarget(String(raw)).toLowerCase()

            ? `saved as ${shownVal}`

            : 'saved';

          setTimeout(() => { statusEl.textContent = ''; }, 2400);

        }

      } catch (e) {

        if (statusEl) statusEl.textContent = 'error';

      }

    };

  }



  // Wire notification email Save button

  const notifyEmailSaveBtn = document.getElementById(`notify-email-save-${projectId}`);

  if (notifyEmailSaveBtn) {

    notifyEmailSaveBtn.onclick = async () => {

      const inp = document.getElementById(`notify-email-${projectId}`);

      const statusEl = document.getElementById(`notify-email-status-${projectId}`);

      const raw = (inp ? inp.value : '').trim() || null;

      try {

        await api(`/projects/${projectId}/ntfy`, {

          method: 'PATCH',

          body: JSON.stringify({ notify_email: raw }),

        });

        if (statusEl) {

          statusEl.textContent = 'saved';

          setTimeout(() => { statusEl.textContent = ''; }, 2400);

        }

      } catch (e) {

        if (statusEl) statusEl.textContent = 'error';

      }

    };

  }



  // Wire notification Test button

  const ntfyTestBtn = document.getElementById(`ntfy-test-${projectId}`);

  if (ntfyTestBtn) {

    ntfyTestBtn.onclick = async () => {

      const statusEl = document.getElementById(`ntfy-status-${projectId}`);

      ntfyTestBtn.disabled = true;

      try {

        await api(`/projects/${projectId}/notify/test`, { method: 'POST', body: '{}' });

        if (statusEl) { statusEl.textContent = 'sent!'; setTimeout(() => { statusEl.textContent = ''; }, 3000); }

      } catch (e) {

        if (statusEl) {

          const raw = String(e?.message || e || '');

          let msg = raw.replace(/^\d+:\s*/, '');

          try {

            const parsed = JSON.parse(msg);

            msg = parsed.detail || parsed.message || parsed.error || msg;

          } catch (_err) {

            // Keep the plain text message when the server did not return JSON.

          }

          statusEl.textContent = msg.includes('No notify URL configured') ? 'save a URL first' : msg;

        }

      } finally {

        ntfyTestBtn.disabled = false;

      }

    };

  }



  // Wire ntfy security warning acknowledgement checkbox

  const ntfyWarnAckCb = document.getElementById(`ntfy-warn-ack-${projectId}`);

  if (ntfyWarnAckCb) {

    ntfyWarnAckCb.onchange = () => {

      if (!ntfyWarnAckCb.checked) return;

      try { localStorage.setItem(STORAGE_KEY('ntfy.warn.dismissed'), '1'); } catch(e) {}

      const warnEl = document.getElementById(`ntfy-warn-${projectId}`);

      if (warnEl) warnEl.style.display = 'none';

      const inp = document.getElementById(`ntfy-url-${projectId}`);

      const saveBtn = document.getElementById(`ntfy-save-${projectId}`);

      const testBtn = document.getElementById(`ntfy-test-${projectId}`);

      [inp, saveBtn, testBtn].forEach(el => { if (el) { el.disabled = false; el.style.opacity = '1'; } });

    };

  }



  body.querySelectorAll('input[data-pref]').forEach(cb => {

    cb.onchange = async () => {

      const statusEl = document.getElementById(`settings-save-status-${projectId}`);

      const payload = {};

      body.querySelectorAll('input[data-pref]').forEach(c => { payload[c.dataset.pref] = c.checked; });

      try {

        await api('/settings/notifications', { method: 'PATCH', body: JSON.stringify(payload) });

        if (statusEl) { statusEl.textContent = 'saved'; setTimeout(() => { statusEl.textContent = ''; }, 1800); }

      } catch (e) {

        if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e))}`;

      }

    };

  });



  // Wire GitHub OAuth connect button

  const ghConnectBtn = document.getElementById(`github-connect-btn-${projectId}`);

  if (ghConnectBtn) {

    ghConnectBtn.onclick = () => {

      window.location.href = `/auth/github/repo-connect?project_id=${encodeURIComponent(projectId)}`;

    };

  }



  // Wire GitHub repo save button (repo + branch only; token is already stored)

  const ghSaveBtn = document.getElementById(`github-save-btn-${projectId}`);

  if (ghSaveBtn) {

    ghSaveBtn.onclick = async () => {

      const statusEl = document.getElementById(`github-status-${projectId}`);

      const repo = (document.getElementById(`github-repo-${projectId}`)?.value || '').trim();

      const branch = (document.getElementById(`github-branch-${projectId}`)?.value || 'main').trim();

      if (!repo) {

        if (statusEl) statusEl.textContent = 'repo is required';

        return;

      }

      ghSaveBtn.disabled = true;

      ghSaveBtn.textContent = 'Saving…';

      if (statusEl) statusEl.textContent = '';

      try {

        await api(`/projects/${projectId}/github/connect`, {

          method: 'POST',

          body: JSON.stringify({ repo, branch }),

        });

        loadSettingsTab(projectId);

      } catch (e) {

        if (statusEl) statusEl.textContent = e.message || 'Save failed';

      } finally {

        ghSaveBtn.disabled = false;

        ghSaveBtn.textContent = 'Save repo';

      }

    };

  }



  // Wire GitHub disconnect button

  const ghDisconnectBtn = document.getElementById(`github-disconnect-btn-${projectId}`);

  if (ghDisconnectBtn) {

    ghDisconnectBtn.onclick = async () => {

      const statusEl = document.getElementById(`github-status-${projectId}`);

      ghDisconnectBtn.disabled = true;

      try {

        await api(`/projects/${projectId}/github/disconnect`, { method: 'DELETE' });

        loadSettingsTab(projectId);

      } catch (e) {

        if (statusEl) statusEl.textContent = 'error disconnecting';

        ghDisconnectBtn.disabled = false;

      }

    };

  }



  // GH-1 — Branch is a dropdown populated from the repo's live branch list

  // (server falls back to common defaults if GitHub is unreachable). Refetches

  // whenever the selected repo changes, keeping the saved branch selected.

  const ghRepoSelect = document.getElementById(`github-repo-${projectId}`);

  const ghBranchSelect = document.getElementById(`github-branch-${projectId}`);

  if (ghBranchSelect) {

    const fillBranches = async (repo, preferred) => {

      const fallback = preferred || ghSelectedBranch || 'main';

      try {

        const res = await api(`/projects/${projectId}/github/branches?repo=${encodeURIComponent(repo || '')}`);

        let branches = Array.isArray(res && res.branches) ? res.branches.slice() : [];

        const want = preferred || (res && res.default_branch) || fallback;

        if (want && !branches.includes(want)) branches.unshift(want);

        if (!branches.length) branches = [fallback];

        ghBranchSelect.innerHTML = branches.map(b =>

          `<option value="${escapeHtml(b)}" ${b === want ? 'selected' : ''}>${escapeHtml(b)}</option>`

        ).join('');

      } catch (e) {

        // Leave the current single-option select in place on failure.

      }

    };

    fillBranches(ghSelectedRepo, ghSelectedBranch);

    if (ghRepoSelect) {

      ghRepoSelect.addEventListener('change', () => {

        const selectedRepo = ghRepoSelect.value;

        const nextDefault = ghRepoMap[selectedRepo] && ghRepoMap[selectedRepo].default_branch;

        fillBranches(selectedRepo, nextDefault);

      });

    }

  }



  // Wire GitHub test button

  const ghTestBtn = document.getElementById(`github-test-btn-${projectId}`);

  if (ghTestBtn) {

    ghTestBtn.onclick = async () => {

      const statusEl = document.getElementById(`github-status-${projectId}`);

      ghTestBtn.disabled = true;

      try {

        const st = await api(`/projects/${projectId}/github/status`);

        if (statusEl) {

          statusEl.textContent = st.connected

            ? (st.github_user ? `@${st.github_user}` : 'connected')

            : 'not connected';

          setTimeout(() => { statusEl.textContent = ''; }, 3000);

        }

      } catch (e) {

        if (statusEl) statusEl.textContent = 'error';

      } finally {

        ghTestBtn.disabled = false;

      }

    };

  }

  } catch (e) {
    body.innerHTML = `<div style="color:var(--error);font-size:11px">Failed to load settings: ${escapeHtml(String(e))}</div>`;
  }

}

// --- esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { suggestNtfyTopic, loadSettingsTab }); } catch (e) {}
