// dashboard-demo.js — extracted from dashboard.js
// Dependency trace: isHostedMode, isHostedAdmin, hideDemoAdminControls, ensureFeedbackButton
// have no category-C deps (A/B only).
// isDemoMode (state), ensureTourButton (startDemoTour), showDemoReadonlyToast (toast),
// showDemoOnboardingOverlay (_demoTourDone etc), showFeedbackModal (api) remain in dashboard.js.

function isHostedMode() {

  return !!window.MERIDIAN_HOSTED;

}



function isHostedAdmin() {

  return isHostedMode() && !!window.MERIDIAN_IS_ADMIN;

}



// Small, unobtrusive "Send feedback" affordance in the sidebar footer. Tagged

// data-demo-hide so the demo's hideDemoAdminControls() sweep removes it (it

// POSTs to a write endpoint). Opens a lightweight modal — bug/feature/other.

function ensureFeedbackButton() {

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



function hideDemoAdminControls() {

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
