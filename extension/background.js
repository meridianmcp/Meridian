// Meridian LaTeX — background service worker (prototype, v0.0.1)
// No backend wired yet: today this just relays popup<->content-script messages.
// Next real step, once the engine has an HTTP endpoint (see ../engine): POST the
// text from MERIDIAN_LATEX_GET_TEXT to that endpoint and return the structural
// outline instead of raw text.

chrome.runtime.onInstalled.addListener(() => {
  console.log("[meridian-latex] extension installed");
});
