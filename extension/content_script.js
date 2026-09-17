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

function injectMainWorldScript() {
  if (document.documentElement.dataset.meridianLatexInjected) return;
  document.documentElement.dataset.meridianLatexInjected = "1";
  const script = document.createElement("script");
  script.src = chrome.runtime.getURL("injected.js");
  document.documentElement.appendChild(script);
  script.remove();
}

/** Relays a doc-info request into the page's MAIN world and waits for
 * injected.js's response, with a timeout so a broken/missing injected
 * script never hangs the popup forever. Resolves (never rejects) to a
 * `{found, ...}` shaped object either way. */
function getCM6Info() {
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
      if (!msg || msg.source !== CM6_RESPONSE_SOURCE || msg.type !== CM6_RESPONSE_TYPE) return;
      const { source: _source, type: _type, ...info } = msg;
      finish(info);
    }

    const timer = setTimeout(() => {
      finish({ found: false, reason: "Timed out waiting for the injected page script to respond." });
    }, CM6_PROBE_TIMEOUT_MS);

    window.addEventListener("message", onMessage);
    injectMainWorldScript();
    window.postMessage({ source: CM6_REQUEST_SOURCE, type: CM6_REQUEST_TYPE }, window.location.origin);
  });
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
  return false;
});
