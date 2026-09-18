// Meridian LaTeX — Overleaf content script (prototype, v0.0.1)
//
// VERIFICATION NEEDED (real, not hypothetical): Overleaf's editor is CodeMirror 6
// (confirmed from Overleaf's own open-source repo, services/web/frontend/js/features/
// source-editor/, as of their 2023 editor rewrite). CM6 renders the editable document
// as a contenteditable `.cm-content` element containing one `.cm-line` div per source
// line. The selectors below are built from that public documentation, NOT verified
// against a live, logged-in Overleaf session in this environment (Overleaf project
// content sits behind auth this script has no access to and should never try to
// obtain). First real next step: load this unpacked in Chrome, open one of your own
// real Overleaf projects, and confirm `.cm-content .cm-line` actually yields the
// document text below — if Overleaf's DOM has since changed, this selector is the
// one place to fix.

function readEditorText() {
  const content = document.querySelector(".cm-content");
  if (!content) return null;
  const lines = Array.from(content.querySelectorAll(".cm-line"));
  if (lines.length === 0) return null;
  return lines.map((el) => el.textContent).join("\n");
}

function detectEditor() {
  const text = readEditorText();
  return {
    editorFound: text !== null,
    lineCount: text ? text.split("\n").length : 0,
    charCount: text ? text.length : 0,
  };
}

// --- MAIN-world bridge (read-only CM6 probe) ---------------------------
//
// This content script runs in Chrome's isolated world: it shares the page's
// DOM but not its JS globals, so it cannot see the properties CM6 attaches
// to DOM nodes and cannot meaningfully call CM6's static
// `EditorView.findFromDOM`. injected.js runs the same probe from the page's
// own MAIN world instead. Getting it there uses the standard MV3
// content-script pattern: append a <script src="chrome-extension://...">
// tag to the page, which Chrome executes as page-authored (MAIN-world)
// script regardless of where the URL points, then remove the tag
// immediately -- the fetch/execution already started by the time it's
// removed. See the pinned Meridian decision "meridian-latex Overleaf
// write-back: EditorView.findFromDOM + MAIN-world dispatch is the safe
// path..." (2026-09-17, project meridian-build).
//
// The two worlds exchange only plain, structured-clone-safe data via
// window.postMessage -- never the EditorView object, never a DOM node.
const CM6_REQUEST_TYPE = "meridian-latex-get-doc-info";
const CM6_RESPONSE_TYPE = "meridian-latex-doc-info";
const CM6_REQUEST_SOURCE = "meridian-latex-content";
const CM6_RESPONSE_SOURCE = "meridian-latex-injected";
const CM6_PROBE_TIMEOUT_MS = 2000;

// Write-dispatch primitive (see injected.js's applyEdits/write-back-spec.md).
// Same MAIN-world bridge, a different request/response message pair.
const APPLY_EDITS_REQUEST_TYPE = "meridian-latex-apply-edits";
const APPLY_EDITS_RESPONSE_TYPE = "meridian-latex-apply-edits-result";
const APPLY_EDITS_TIMEOUT_MS = 2000;

// Read-only line-info primitive (see injected.js's getLineInfo()). Used by
// the real node-editing flow (popup.js) to get a claimed node's live line
// text + its exact document-offset start, so it can compute a precise
// {from, to} for the specific field being edited before calling
// MERIDIAN_LATEX_APPLY_EDITS above. Same MAIN-world bridge, a third
// request/response message pair.
const LINE_INFO_REQUEST_TYPE = "meridian-latex-get-line-info";
const LINE_INFO_RESPONSE_TYPE = "meridian-latex-line-info-result";
const LINE_INFO_TIMEOUT_MS = 2000;

// BUG FIXED 2026-09-17 (found by re-reading this code after a real, live
// timeout report -- "Timed out waiting for the injected page script to
// respond"): this used to append the <script> tag and fire
// window.postMessage(...) on the very next line, synchronously. Loading an
// externally-sourced <script src> is ASYNCHRONOUS -- injected.js had not
// actually executed yet (and so had not yet registered its own
// window.addEventListener("message", ...)) by the time the request went
// out, so the very first request after each page load was silently lost to
// nobody listening, and the 2s timeout fired every time. Not flakiness --
// a real race, reproducible on every load. Fixed by making
// injectMainWorldScript() a promise that resolves on the script's own
// `load` event, and only sending the postMessage request after that
// resolves. A cached (already-injected) page resolves immediately.
let mainWorldScriptReady = null;

function injectMainWorldScript() {
  if (mainWorldScriptReady) return mainWorldScriptReady;
  if (document.documentElement.dataset.meridianLatexInjected === "1") {
    mainWorldScriptReady = Promise.resolve();
    return mainWorldScriptReady;
  }
  mainWorldScriptReady = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = chrome.runtime.getURL("injected.js");
    script.onload = () => {
      document.documentElement.dataset.meridianLatexInjected = "1";
      script.remove();
      resolve();
    };
    script.onerror = () => {
      script.remove();
      mainWorldScriptReady = null; // allow a retry on the next call
      reject(new Error("injected.js failed to load"));
    };
    document.documentElement.appendChild(script);
  });
  return mainWorldScriptReady;
}

/**
 * Generic relay: sends a `{source: CM6_REQUEST_SOURCE, type: requestType, ...payload}`
 * postMessage into the page's MAIN world (injecting injected.js first if it
 * hasn't run yet) and waits for a same-window response shaped
 * `{source: CM6_RESPONSE_SOURCE, type: responseType, ...}`, with a timeout so
 * a broken/missing injected script never hangs the popup forever.
 *
 * Resolves (never rejects) either way. On timeout or injection failure it
 * resolves `{...failureShape, reason}` -- `failureShape` lets each caller
 * pick the right "nothing happened" shape for its own protocol (`{found:
 * false}` for the read probe, `{applied: false}` for the write-dispatch
 * primitive) without this helper needing to know which one it's relaying.
 *
 * Shared by getCM6Info() (read probe) and applyEditsViaMainWorld() (write
 * dispatch) -- both request/response pairs use this same bridge mechanics,
 * only the message types, payload, and failure shape differ.
 */
function relayToMainWorld(requestType, responseType, payload, failureShape, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;

    function finish(result) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve(result);
    }

    function onMessage(event) {
      // Standard postMessage hygiene: only trust same-window messages
      // shaped exactly like our own protocol.
      if (event.source !== window) return;
      const msg = event.data;
      if (!msg || msg.source !== CM6_RESPONSE_SOURCE || msg.type !== responseType) return;
      const { source: _source, type: _type, ...info } = msg;
      finish(info);
    }

    const timer = setTimeout(() => {
      finish({ ...failureShape, reason: "Timed out waiting for the injected page script to respond." });
    }, timeoutMs);

    window.addEventListener("message", onMessage);
    injectMainWorldScript()
      .then(() => {
        window.postMessage(
          Object.assign({ source: CM6_REQUEST_SOURCE, type: requestType }, payload),
          window.location.origin,
        );
      })
      .catch((err) => {
        finish({ ...failureShape, reason: `Failed to inject page script: ${err.message}` });
      });
  });
}

/** Relays a doc-info request into the page's MAIN world. Resolves to a
 * `{found, ...}` shaped object. */
function getCM6Info() {
  return relayToMainWorld(CM6_REQUEST_TYPE, CM6_RESPONSE_TYPE, {}, { found: false }, CM6_PROBE_TIMEOUT_MS);
}

/** Relays a write-dispatch request (`edits: [{from, to, insert}, ...]`) into
 * the page's MAIN world. Resolves to `{applied, verified?, newLength?}` on
 * success or `{applied: false, reason}` on any failure -- see injected.js's
 * applyEdits() for the validation/dispatch/readback-verification logic that
 * produces this. */
function applyEditsViaMainWorld(edits) {
  return relayToMainWorld(
    APPLY_EDITS_REQUEST_TYPE,
    APPLY_EDITS_RESPONSE_TYPE,
    { edits },
    { applied: false },
    APPLY_EDITS_TIMEOUT_MS,
  );
}

/** Relays a line-info request (1-indexed `lineNumber`, matching CM6's
 * state.doc.line(n) convention) into the page's MAIN world. Resolves to
 * `{found, from, to, text}` on success or `{found: false, reason}` on any
 * failure -- see injected.js's getLineInfo(). Read-only, never dispatches. */
function getLineInfoViaMainWorld(lineNumber) {
  return relayToMainWorld(
    LINE_INFO_REQUEST_TYPE,
    LINE_INFO_RESPONSE_TYPE,
    { lineNumber },
    { found: false },
    LINE_INFO_TIMEOUT_MS,
  );
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "MERIDIAN_LATEX_GET_STATUS") {
    sendResponse(detectEditor());
    return true;
  }
  if (message?.type === "MERIDIAN_LATEX_GET_TEXT") {
    sendResponse({ text: readEditorText() });
    return true;
  }
  if (message?.type === "MERIDIAN_LATEX_GET_CM6_INFO") {
    getCM6Info().then(sendResponse);
    return true;
  }
  if (message?.type === "MERIDIAN_LATEX_APPLY_EDITS") {
    applyEditsViaMainWorld(message.edits).then(sendResponse);
    return true;
  }
  if (message?.type === "MERIDIAN_LATEX_GET_LINE_INFO") {
    getLineInfoViaMainWorld(message.lineNumber).then(sendResponse);
    return true;
  }
  return false;
});
