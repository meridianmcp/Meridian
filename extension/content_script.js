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

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "MERIDIAN_LATEX_GET_STATUS") {
    sendResponse(detectEditor());
    return true;
  }
  if (message?.type === "MERIDIAN_LATEX_GET_TEXT") {
    sendResponse({ text: readEditorText() });
    return true;
  }
  return false;
});
