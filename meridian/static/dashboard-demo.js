// Extracted from dashboard.js -- demo/tour/utility functions
// DO NOT DELETE -- loaded via script tag in dashboard.html

export function isDemoMode() {

  return !!state.serverConfig?.demo_mode || window.location.pathname.startsWith('/demo');

}

export function isHostedMode() {

  return !!window.MERIDIAN_HOSTED;

}

export function isHostedAdmin() {

  return isHostedMode() && !!window.MERIDIAN_IS_ADMIN;

}

export function ensureTourButton() {

  const footer = document.querySelector('.sidebar-footer');

  if (!footer || document.getElementById('tour-launch-btn')) return;

  const btn = document.createElement('button');

  btn.id = 'tour-launch-btn';

  btn.type = 'button';

  btn.textContent = '🧭 Take the tour';

  btn.title = 'Replay the guided dashboard walkthrough';

  btn.style = 'display:block;width:100%;margin-top:8px;padding:6px 10px;font-size:11px;color:var(--text);font-family:var(--font-mono);text-align:center;background:var(--surface-1);border:1px solid var(--border);border-radius:5px;cursor:pointer';

  btn.onmouseenter = () => { btn.style.borderColor = 'var(--accent)'; btn.style.color = 'var(--accent)'; };

  btn.onmouseleave = () => { btn.style.borderColor = 'var(--border)'; btn.style.color = 'var(--text)'; };

  btn.onclick = () => { try { startDemoTour(0); } catch (e) {} };

  footer.appendChild(btn);

}

export function ensureFeedbackButton() {

  // The /feedback endpoint is hosted-only (404 otherwise) — don't show a

  // button that would fail on self-hosted instances.

  if (!isHostedMode()) return;

  const footer = document.querySelector('.sidebar-footer');

  if (!footer || document.getElementById('feedback-launch-btn')) return;

  const btn = document.createElement('button');

  btn.id = 'feedback-launch-btn';

  btn.type = 'button';

  btn.setAttribute('data-demo-hide', '');

  btn.textContent = '💬 Send feedback';

  btn.title = 'Report a bug or request a feature';

  btn.style = 'display:block;width:100%;margin-top:6px;padding:5px 10px;font-size:10px;color:var(--muted);font-family:var(--font-mono);text-align:center;background:transparent;border:1px solid var(--border);border-radius:5px;cursor:pointer';

  btn.onmouseenter = () => { btn.style.borderColor = 'var(--accent)'; btn.style.color = 'var(--accent)'; };

  btn.onmouseleave = () => { btn.style.borderColor = 'var(--border)'; btn.style.color = 'var(--muted)'; };

  btn.onclick = () => { try { showFeedbackModal(); } catch (e) {} };

  footer.appendChild(btn);

}

export function showFeedbackModal() {

  if (document.getElementById('feedback-modal')) return;

  const overlay = document.createElement('div');

  overlay.id = 'feedback-modal';

  overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center';

  const box = document.createElement('div');

  box.style.cssText = 'background:var(--surface-0);border:1px solid var(--border);border-radius:8px;padding:24px 28px;width:440px;max-width:94vw;display:flex;flex-direction:column;gap:12px';

  box.innerHTML = `

    <div style="font-weight:700;font-size:14px">Send feedback</div>

    <label style="font-size:11px;color:var(--muted)">Type

      <select id="feedback-type" style="display:block;width:100%;margin-top:4px;font-size:12px;font-family:var(--font-mono);background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:6px 8px">

        <option value="bug">Bug</option>

        <option value="feature">Feature request</option>

        <option value="other">Other</option>

      </select>

    </label>

    <label style="font-size:11px;color:var(--muted)">Message

      <textarea id="feedback-message" rows="4" placeholder="What's on your mind?" style="display:block;width:100%;margin-top:4px;box-sizing:border-box;font-size:12px;font-family:inherit;background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:7px 10px;resize:vertical"></textarea>

    </label>

    <label style="font-size:11px;color:var(--muted)">Email

      <input id="feedback-email" type="email" placeholder="you@example.com" style="display:block;width:100%;margin-top:4px;box-sizing:border-box;font-size:12px;font-family:var(--font-mono);background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:6px 10px">

    </label>

    <div id="feedback-status" style="font-size:11px;min-height:16px;color:var(--muted)"></div>

    <div style="display:flex;gap:8px;justify-content:flex-end">

      <button id="feedback-cancel" class="secondary" style="font-size:12px">Cancel</button>

      <button id="feedback-send" style="font-size:12px">Send</button>

    </div>`;

  overlay.appendChild(box);

  document.body.appendChild(overlay);

  const statusEl = box.querySelector('#feedback-status');

  box.querySelector('#feedback-cancel').onclick = () => overlay.remove();

  overlay.onclick = e => { if (e.target === overlay) overlay.remove(); };

  box.querySelector('#feedback-send').onclick = async () => {

    const type = box.querySelector('#feedback-type').value;

    const message = box.querySelector('#feedback-message').value.trim();

    const email = box.querySelector('#feedback-email').value.trim();

    if (!message) { statusEl.textContent = 'Enter a message.'; statusEl.style.color = 'var(--danger,#dc2626)'; return; }
    if (!email || !email.includes('@')) { statusEl.textContent = 'Enter a valid email.'; statusEl.style.color = 'var(--danger,#dc2626)'; return; }

    statusEl.textContent = 'Sending…'; statusEl.style.color = 'var(--muted)';

    try {

      await api('/feedback', { method: 'POST', body: JSON.stringify({ type, message, email }) });

      statusEl.textContent = 'Thanks! Feedback sent.'; statusEl.style.color = '#059669';

      setTimeout(() => overlay.remove(), 900);

    } catch (e) {

      statusEl.textContent = e.message || 'Could not send — please try again.';

      statusEl.style.color = 'var(--danger,#dc2626)';

    }

  };

  box.querySelector('#feedback-message').focus();

}

export function hideDemoAdminControls() {

  const selectors = [

    '#restart-server-btn',

    '#stop-server-btn',

    '#banner-restart-btn',

    '#git-check-btn',

    '#update-banner',

    '#delete-account-section',

    '[data-demo-hide]',

    // Settings: hide write controls entirely in demo

    '[id^="ntfy-url-"]',

    '[id^="ntfy-save-"]',

    '[id^="ntfy-test-"]',

    '[id^="ntfy-status-"]',

    '[id^="mcp-gen-token-"]',

    '[id^="invite-email-"]',

    '[id^="invite-role-"]',

    '[id^="invite-btn-"]',

    '[id^="github-repo-"]',

    '[id^="github-branch-"]',

    '[id^="github-connect-btn-"]',

    '[id^="github-save-btn-"]',

    '[id^="github-disconnect-btn-"]',

    '[id^="github-test-btn-"]',

    // Files tab: hide Edit subtab, show Preview only

    '[id^="file-mode-edit-"]',

    // Workspace settings: hide write controls entirely in demo (v3.4)

    '#ws-settings-save',

    '#ws-dec-title', '#ws-dec-body', '#ws-dec-add',

    '#ws-note-title', '#ws-note-body', '#ws-note-add',

    // Workspace DB connect UI — write action, hide in demo

    '#connect-db-link',

    '#connect-db-save',

    // Easy-setup + goal template write controls (settings tab)

    '[id^="exec-ez-save-"]',

    '[id^="codex-save-goal-"]',

    '[id^="codex-regen-goal-"]',

  ];

  selectors.forEach(sel => {

    document.querySelectorAll(sel).forEach(el => { el.style.display = 'none'; });

  });



  // Apply 'Sign in to use' tooltip + not-allowed cursor to write-action buttons

  // so demo visitors know what to expect before clicking.

  const writeBtnSelectors = [

    'button[data-write]',

    '.btn-write',

    '#add-sprint-item-btn',

    '[id^="save-goal-"]',

    '[id^="save-sprint-"]',

    '[id^="delete-project-"]',

    '[id^="rename-project-"]',

    '[id^="add-sprint-"]',

    '[id^="mark-done-"]',

    '[id^="claim-task-"]',

    // HITL markdown section-update approval (writes + commits a doc) — demo-gated

    '.hitl-approve-btn',

    '.hitl-reject-btn',

  ];

  writeBtnSelectors.forEach(sel => {

    document.querySelectorAll(sel).forEach(btn => {

      if (btn.dataset.demoHintApplied) return;

      btn.dataset.demoHintApplied = '1';

      btn.title = 'Sign in to use';

      btn.style.opacity = String(parseFloat(btn.style.opacity || '1') * 0.55);

      btn.style.cursor = 'not-allowed';

      const orig = btn.onclick;

      btn.onclick = (e) => { e.preventDefault(); e.stopPropagation(); showDemoReadonlyToast(); };

    });

  });

}

export function showDemoReadonlyToast() {

  const el = document.getElementById('toast');

  el.innerHTML = 'Read-only demo — <a href="/auth/login" style="color:#fff;font-weight:600;text-decoration:underline">sign in for full access →</a>';

  el.classList.add('error', 'show');

  clearTimeout(toast._t);

  toast._t = setTimeout(() => el.classList.remove('show'), 3200);

}

export function showDemoOnboardingOverlay() {

  if (document.getElementById('demo-onboarding-overlay')) return;

  // Once the visitor has finished the tour, never auto-show onboarding again.

  if (_demoTourDone()) return;

  const resuming = _demoTourSavedStep() > 0;

  const ctaLabel = resuming ? 'Resume tour →' : 'Got it — show me around →';

  const overlay = document.createElement('div');

  overlay.id = 'demo-onboarding-overlay';

  overlay.style = 'position:fixed;inset:0;z-index:20000;background:rgba(0,0,0,0.72);display:flex;align-items:center;justify-content:center;padding:16px';

  overlay.innerHTML = `<div style="background:#1e2029;border:1px solid #7c3aed66;border-radius:14px;padding:32px 36px;max-width:480px;width:100%;box-shadow:0 12px 48px rgba(0,0,0,0.7);position:relative;font-family:inherit">

  <button onclick="document.getElementById('demo-onboarding-overlay').remove()" style="position:absolute;top:12px;right:14px;background:none;border:none;color:#8b8fa8;font-size:22px;cursor:pointer;line-height:1;padding:4px" title="Dismiss">×</button>

  <h3 style="color:#e8eaf0;margin:0 0 18px;font-size:1.35rem;font-weight:700">Welcome to the Meridian demo</h3>

  <ol style="color:#c4c6d4;font-size:1.02rem;line-height:1.85;padding-left:1.3em;margin:0 0 24px">

    <li>This is a live demo coordinating a real multi-session build. It's read-only.</li>

    <li>Click any session on the left to explore.</li>

    <li>Write actions are disabled — <a href="/auth/login" style="color:#6c8fff;text-decoration:underline">sign in to create your own project</a>.</li>

  </ol>

  <div style="display:flex;gap:8px">

    <button onclick="document.getElementById('demo-onboarding-overlay').remove()" style="background:#2a2d35;border:none;border-radius:7px;color:#8b8fa8;padding:10px 18px;cursor:pointer;font-size:.98rem;font-family:inherit;flex:0 0 auto">Skip</button>

    <button onclick="document.getElementById('demo-onboarding-overlay').remove();resumeDemoTour()" style="background:#7c3aed;border:none;border-radius:7px;color:#fff;padding:10px 24px;cursor:pointer;font-size:1.02rem;font-family:inherit;flex:1">${ctaLabel}</button>

  </div>

</div>`;

  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  document.body.appendChild(overlay);

}

// --- ITEM 4 esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { isDemoMode, isHostedMode, isHostedAdmin, ensureTourButton, ensureFeedbackButton, showFeedbackModal, hideDemoAdminControls, showDemoReadonlyToast, showDemoOnboardingOverlay }); } catch (e) {}
