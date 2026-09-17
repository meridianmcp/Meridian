// Meridian LaTeX — background service worker (prototype, v0.0.1)
// The engine call (POST /outline) now happens directly from popup.js, not
// routed through here -- MV3 extension pages get the same host_permissions
// fetch access as any other extension context, so there's no need for
// message-passing through the service worker just to reach localhost.
// This file is currently just a lifecycle log; kept as the place a future
// always-on behavior (e.g. auto-refreshing the outline on tab focus) would
// go, since content scripts and popups don't persist between uses.

chrome.runtime.onInstalled.addListener(() => {
  console.log("[meridian-latex] extension installed");
});
