// Meridian LaTeX — MAIN-world injected script (read-only CM6 probe, prototype v0.0.1)
//
// Runs in the page's own MAIN JS world, loaded by content_script.js via a
// page-appended <script src="chrome-extension://.../injected.js"> tag (the
// standard MV3 pattern for reaching MAIN world without a background-driven
// chrome.scripting.executeScript call -- see content_script.js). Grounded in
// the pinned Meridian decision "meridian-latex Overleaf write-back:
// EditorView.findFromDOM + MAIN-world dispatch is the safe path..."
// (2026-09-17, project meridian-build): a normal isolated-world content
// script cannot see the properties CM6 attaches to its DOM nodes, and can't
// call EditorView.findFromDOM meaningfully either -- Chrome gives each world
// its own property bag for the same DOM node. This script is the MAIN-world
// half; content_script.js is the isolated-world half. They only exchange
// plain, structured-clone-safe data via window.postMessage -- never the
// EditorView object itself, never a DOM node.
//
// READ-ONLY, DELIBERATELY. No view.dispatch() call anywhere in this file, no
// write/edit capability at all -- the pinned decision above is explicit that
// the write half needs a disposable-test-project validation pass Adam drives
// himself before any of that code exists. This file only proves the
// find-a-live-EditorView + read-its-state mechanism works.

(function () {
  const REQUEST_TYPE = "meridian-latex-get-doc-info";
  const RESPONSE_TYPE = "meridian-latex-doc-info";
  const RESPONSE_SOURCE = "meridian-latex-injected";

  /**
   * The one piece of this scaffold NOT fully nailed down by the source
   * research behind the pinned decision: getting a reference to the
   * EditorView *class itself* (not an instance) so its static
   * `findFromDOM` can be called free of any cross-world instanceof
   * mismatch. Whether Overleaf's own bundle exposes the class on a global
   * (e.g. something like `window.CodeMirror`) is UNCONFIRMED -- this task
   * has no live browser access to check empirically.
   *
   * What CM6 does document (and what does not depend on Overleaf exposing
   * anything): once the editor has rendered, its `.cm-content` DOM node
   * (and ancestors up to `.cm-editor`) carries a `cmView` property back to
   * CM6's internal ContentView, whose `.view` is a live EditorView
   * instance. Every instance carries `.constructor` back to its own class,
   * which is enough to reach the static `findFromDOM` without ever
   * importing @codemirror/view ourselves (importing a separately-bundled
   * copy is exactly the cross-world identity mismatch the pinned decision
   * warns `dispatch()` breaks on -- avoided here on principle even though
   * this pass never dispatches).
   *
   * This property walk (`cmView.view`) is undocumented CM6 internals and
   * is the most likely single point to drift across CM6 versions -- it is
   * used here ONLY to recover the class reference for one findFromDOM
   * call, never to read document state directly. If it breaks in a future
   * CM6 version, getDocInfo() below reports `found: false` with a clear
   * reason instead of throwing.
   */
  function getEditorViewClass(startEl) {
    let node = startEl;
    for (let hops = 0; node && hops < 6; hops++) {
      const cmView = node.cmView;
      const view = cmView && cmView.view;
      if (view && view.constructor && typeof view.constructor.findFromDOM === "function") {
        return view.constructor;
      }
      node = node.parentElement;
    }
    return null;
  }

  function getDocInfo() {
    try {
      const cmContent = document.querySelector(".cm-content");
      if (!cmContent) {
        return { found: false, reason: "No .cm-content element on the page." };
      }

      const EditorViewClass = getEditorViewClass(cmContent);
      if (!EditorViewClass) {
        return {
          found: false,
          reason:
            "Could not reach the EditorView class via .cm-content's cmView property chain " +
            "-- CM6 internals may have changed, or the editor has not fully rendered yet.",
        };
      }

      const view = EditorViewClass.findFromDOM(cmContent);
      if (!view) {
        return { found: false, reason: "EditorView.findFromDOM(.cm-content) returned null." };
      }

      // Read-only sanity check. Never dispatch, never mutate view.state.
      const doc = view.state.doc;
      return {
        found: true,
        length: doc.length,
        lines: doc.lines,
        preview: doc.sliceString(0, 200),
      };
    } catch (err) {
      return {
        found: false,
        reason: `Unexpected error reading CM6 state: ${err && err.message ? err.message : String(err)}`,
      };
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const msg = event.data;
    if (!msg || msg.source !== "meridian-latex-content" || msg.type !== REQUEST_TYPE) return;

    const info = getDocInfo();
    window.postMessage(
      Object.assign({ source: RESPONSE_SOURCE, type: RESPONSE_TYPE }, info),
      window.location.origin,
    );
  });
})();
