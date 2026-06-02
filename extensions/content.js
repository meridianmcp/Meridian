// Meridian Checkpoint — content script for claude.ai
// Watches for "checkpoint" or "save progress" in user messages,
// fires POST /hooks/stop to the configured Meridian server.

(function () {
  'use strict';

  let config = { meridianUrl: 'http://localhost:7878', projectId: '', enabled: true };

  // Load config from storage
  chrome.storage.sync.get(['meridianUrl', 'projectId', 'enabled'], (items) => {
    if (items.meridianUrl) config.meridianUrl = items.meridianUrl;
    if (items.projectId) config.projectId = items.projectId;
    if (items.enabled !== undefined) config.enabled = items.enabled;
  });

  // Listen for config changes
  chrome.storage.onChanged.addListener((changes) => {
    if (changes.meridianUrl) config.meridianUrl = changes.meridianUrl.newValue;
    if (changes.projectId) config.projectId = changes.projectId.newValue;
    if (changes.enabled !== undefined) config.enabled = changes.enabled.newValue;
  });

  const TRIGGER_PHRASES = ['checkpoint', 'save progress', '/meridian'];

  function containsTrigger(text) {
    const lower = text.toLowerCase();
    return TRIGGER_PHRASES.some(phrase => lower.includes(phrase));
  }

  async function fireCheckpoint(reason) {
    if (!config.projectId) {
      console.warn('[Meridian] project_id not set — open extension popup to configure');
      return;
    }
    if (!config.enabled) return;

    const url = `${config.meridianUrl.replace(/\/$/, '')}/hooks/stop`;
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: config.projectId }),
      });
      if (resp.ok) {
        console.log(`[Meridian] Checkpoint fired (${reason})`);
      } else {
        console.warn(`[Meridian] Checkpoint failed: ${resp.status}`);
      }
    } catch (err) {
      console.warn('[Meridian] Checkpoint error:', err.message);
    }
  }

  // Watch for user messages submitted in the claude.ai textarea
  // Uses a MutationObserver to detect when new user message elements appear in the DOM
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        // claude.ai user messages are in elements with data-testid="user-message"
        // or similar. Check text content for trigger phrases.
        const userMsgEl = node.querySelector('[data-testid="user-message"]') || node;
        const text = userMsgEl.textContent || '';
        if (text.length > 0 && containsTrigger(text)) {
          fireCheckpoint(`trigger phrase in message: "${text.slice(0, 50)}"`);
        }
      }
    }
  });

  // Start observing the main content area
  function startObserving() {
    const target = document.querySelector('main') || document.body;
    observer.observe(target, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startObserving);
  } else {
    startObserving();
  }

  // Listen for messages from background (timer-based checkpoint)
  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === 'TIMER_CHECKPOINT') {
      fireCheckpoint('timer');
    }
  });
})();
