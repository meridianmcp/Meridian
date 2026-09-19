// Meridian LaTeX — background service worker (v0.0.2)
//
// Two jobs live here:
//   1. The engine call (POST /outline) happens directly from popup.js, not
//      routed through here -- MV3 extension pages get the same
//      host_permissions fetch access as any other extension context, so
//      there's no need for message-passing through the service worker
//      just to reach localhost.
//   2. Self-reload: polls the local engine server (GET /extension-version,
//      see engine/src/server.js) for a change fingerprint of extension/
//      and reloads this extension the moment it detects one, so Adam
//      never has to manually click "reload" in chrome://extensions again
//      after editing extension/*.js. See README for the problem this
//      solves and why browser automation can't do it instead
//      (chrome://extensions and chrome-extension:// pages are both
//      off-limits to browser automation tools).
//
// MV3 lifecycle notes (read before touching this file):
// - Service workers are ephemeral: Chrome kills an idle one after ~30s and
//   wakes a fresh one on the next event. A `setInterval` timer dies with
//   the worker and never fires again -- chrome.alarms is the only
//   MV3-correct way to run something periodically regardless of whether
//   the worker was asleep. Alarm state (existence + schedule) lives in the
//   browser, not the worker's in-memory state, so it survives every
//   restart -- exactly what a poll loop that must outlive worker naps
//   needs. That's why this uses chrome.alarms instead of setInterval.
// - chrome.runtime.reload() terminates this execution context immediately
//   -- nothing after that call in the same tick runs. Anything that must
//   happen "after the reload" (refreshing the open Overleaf tabs) has to
//   be persisted to chrome.storage.local first and picked back up the
//   NEXT time this file's top-level code runs.
// - Which listener actually fires reliably after chrome.runtime.reload()?
//   Neither is the right hook:
//     - chrome.runtime.onStartup only fires when the browser/profile
//       itself starts up -- an extension reload is not a browser startup,
//       so this never fires from chrome.runtime.reload().
//     - chrome.runtime.onInstalled is documented for "install", "update",
//       and "chrome_update" reasons. Whether a same-version
//       chrome.runtime.reload() call reliably counts as an "update" isn't
//       part of its documented contract, so depending on it here would be
//       guessing.
//   The one guarantee MV3 actually gives is that a service worker's
//   top-level (module-scope) code runs every time the worker starts, for
//   ANY reason -- reload, alarm wake, browser startup, incoming message.
//   So the pending-tab-refresh check below is a plain top-level call, not
//   something gated behind onStartup/onInstalled.

const ENGINE_BASE_URL = "http://127.0.0.1:8471";
const VERSION_CHECK_ALARM_NAME = "meridian-latex-version-check";
// Requested cadence is ~3s. Chrome's alarms API enforces a minimum period
// for repeating alarms -- historically 1 minute for a packed/published
// extension, relaxed to a much shorter floor for an extension loaded
// unpacked via Developer Mode (exactly how this extension runs today --
// see README, no Web Store listing yet) specifically to make testing
// periodic alarms practical. The unpacked-mode floor has differed across
// Chrome versions, so treat this as "as fast as Chrome's dev-mode floor
// allows," not a literal guarantee -- if Chrome clamps it upward, the poll
// still runs periodically and self-reload still works, just on a slightly
// coarser cadence than 3s.
const VERSION_CHECK_PERIOD_MINUTES = 3 / 60;
const STORAGE_KEY_LAST_HASH = "extensionVersionHash";
const STORAGE_KEY_PENDING_REFRESH = "pendingTabRefresh";

chrome.alarms.create(VERSION_CHECK_ALARM_NAME, {
  periodInMinutes: VERSION_CHECK_PERIOD_MINUTES,
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === VERSION_CHECK_ALARM_NAME) {
    checkForExtensionChange();
  }
});

chrome.runtime.onInstalled.addListener(() => {
  console.log("[meridian-latex] extension installed");
});

/**
 * Polls the local engine server's extension/ fingerprint and compares it
 * to the last-seen value in chrome.storage.local (in-memory state doesn't
 * survive a service worker restart, so the baseline has to live in
 * storage). On a genuine change, snapshots the currently-open Overleaf
 * tabs so they can be refreshed after the reload, then reloads this
 * extension.
 *
 * Must never throw and must never spam the console: the local engine
 * server not running is the COMMON case (Adam doesn't always have it up),
 * and a missed poll cycle is just a silent no-op -- the next alarm tries
 * again in ~3s (see cadence note above).
 */
async function checkForExtensionChange() {
  let hash;
  try {
    const res = await fetch(`${ENGINE_BASE_URL}/extension-version`);
    if (!res.ok) return; // server up but unhappy -- skip this cycle, no log spam
    const body = await res.json();
    hash = body && body.hash;
  } catch {
    return; // server not running / network hiccup -- silent no-op by design
  }
  if (typeof hash !== "string" || !hash) return;

  const stored = await chrome.storage.local.get(STORAGE_KEY_LAST_HASH);
  const lastHash = stored[STORAGE_KEY_LAST_HASH];

  if (lastHash === undefined) {
    // First successful poll ever for this profile/install -- establish the
    // baseline only. Without this, the very first poll after every browser
    // startup would look like "a change just happened" and reload the
    // extension for no reason.
    await chrome.storage.local.set({ [STORAGE_KEY_LAST_HASH]: hash });
    return;
  }

  if (lastHash === hash) return; // nothing changed -- the normal case, do nothing

  // A real change. Save which Overleaf tabs need refreshing BEFORE
  // reloading -- chrome.runtime.reload() ends this execution context on
  // the next line, so nothing after it in this function ever runs; the
  // actual tab reloads happen on the NEXT service worker startup (the
  // flushPendingTabRefresh() call at the bottom of this file).
  let overleafTabs = [];
  try {
    overleafTabs = await chrome.tabs.query({ url: "https://www.overleaf.com/*" });
  } catch {
    overleafTabs = [];
  }
  const tabIds = overleafTabs
    .map((tab) => tab.id)
    .filter((id) => typeof id === "number");

  await chrome.storage.local.set({
    [STORAGE_KEY_LAST_HASH]: hash,
    [STORAGE_KEY_PENDING_REFRESH]: tabIds,
  });

  console.log(
    `[meridian-latex] detected extension change, reloading (${tabIds.length} Overleaf tab(s) queued for refresh)`
  );
  chrome.runtime.reload();
}

/**
 * Runs unconditionally every time this service worker's top-level code
 * executes (see the MV3 lifecycle note above) -- including immediately
 * after chrome.runtime.reload(), which is the one case this exists to
 * handle. A no-op on every other startup, since pendingTabRefresh is only
 * ever set right before a self-triggered reload.
 */
async function flushPendingTabRefresh() {
  const stored = await chrome.storage.local.get(STORAGE_KEY_PENDING_REFRESH);
  const tabIds = stored[STORAGE_KEY_PENDING_REFRESH];
  if (!Array.isArray(tabIds) || tabIds.length === 0) return;

  for (const tabId of tabIds) {
    try {
      await chrome.tabs.reload(tabId);
    } catch {
      // Tab closed since the reload was queued -- nothing to refresh, fine.
    }
  }

  // Clear regardless of per-tab outcome so this never re-fires on a later,
  // unrelated startup.
  await chrome.storage.local.remove(STORAGE_KEY_PENDING_REFRESH);
}

flushPendingTabRefresh();
