// Meridian LaTeX — MAIN-world injected script (CM6 read + write-dispatch primitive)
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
// Two message types are handled here:
//   - meridian-latex-get-doc-info  (read-only sanity probe, unchanged from
//     the original scaffold -- never dispatches, never mutates view.state)
//   - meridian-latex-apply-edits   (NEW -- the write-dispatch primitive)
//
// The write path is genuinely higher-risk: a bug here can corrupt someone's
// live Overleaf document. Per the pinned decision, it is dispatch-only --
// view.dispatch() with a plain-object ChangeSpec array is the ONLY mutation
// path in this file. No synthetic paste/input events, no raw DOM mutation
// (contenteditable text nodes are CM6's own rendering output, not a data
// model -- writing to them directly would desync from CM6's internal state
// without CM6 ever knowing an edit happened). No StateEffect/Facet/Extension
// class instances are constructed anywhere here either -- the pinned
// decision is explicit that constructing those breaks cross-module
// `instanceof` identity when this script's copy of a class doesn't match
// the one Overleaf's own bundled CM6 build compares against internally.
// Plain `{from, to, insert}` objects have no such identity requirement --
// CM6 reads them structurally, not via instanceof -- which is exactly why
// they're the only shape used here.

(function () {
  const REQUEST_TYPE = "meridian-latex-get-doc-info";
  const RESPONSE_TYPE = "meridian-latex-doc-info";
  const APPLY_EDITS_REQUEST_TYPE = "meridian-latex-apply-edits";
  const APPLY_EDITS_RESPONSE_TYPE = "meridian-latex-apply-edits-result";
  const LINE_INFO_REQUEST_TYPE = "meridian-latex-get-line-info";
  const LINE_INFO_RESPONSE_TYPE = "meridian-latex-line-info-result";
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
   * warns `dispatch()` breaks on).
   *
   * This property walk (`cmView.view`) is undocumented CM6 internals and
   * is the most likely single point to drift across CM6 versions -- it is
   * used here ONLY to recover the class reference for one findFromDOM
   * call, never to read document state directly. If it breaks in a future
   * CM6 version, recoverEditorView() below reports a clear reason instead
   * of throwing, for both the read and write paths.
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

  /**
   * Shared recovery path for both the read probe and the write-dispatch
   * handler -- refactored out so there is exactly one place that walks
   * `.cm-content` -> cmView class -> `findFromDOM`, instead of the read and
   * write handlers each duplicating (and potentially drifting on) the same
   * logic. Returns `{view}` on success or `{view: null, reason}` on any
   * failure; never throws.
   */
  function recoverEditorView() {
    const cmContent = document.querySelector(".cm-content");
    if (!cmContent) {
      return { view: null, reason: "No .cm-content element on the page." };
    }

    const EditorViewClass = getEditorViewClass(cmContent);
    if (!EditorViewClass) {
      return {
        view: null,
        reason:
          "Could not reach the EditorView class via .cm-content's cmView property chain " +
          "-- CM6 internals may have changed, or the editor has not fully rendered yet.",
      };
    }

    const view = EditorViewClass.findFromDOM(cmContent);
    if (!view) {
      return { view: null, reason: "EditorView.findFromDOM(.cm-content) returned null." };
    }

    return { view, reason: null };
  }

  function getDocInfo() {
    try {
      const { view, reason } = recoverEditorView();
      if (!view) return { found: false, reason };

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

  /**
   * Validates a batch of `{from, to, insert}` edits against the CURRENT
   * document length, right before dispatch. This is the load-bearing safety
   * check for the whole write primitive -- the caller may have computed
   * `from`/`to` against a doc snapshot read moments (or longer) ago, and the
   * live document is the only thing that matters at dispatch time. Rejects
   * the WHOLE batch (never applies a subset) on the first problem found:
   *
   *   - not an array, or empty
   *   - any edit missing integer `from`/`to` or a string `insert`
   *   - any `from`/`to` outside `[0, doc.length]`
   *   - any edit with `from > to`
   *   - any two edits in the batch whose `[from, to)` ranges overlap
   *
   * The overlap check is not explicitly asked for by name in the spec, but
   * follows directly from it: CM6 treats an array passed to `changes` as a
   * set of SIMULTANEOUS edits against the one current document (not a
   * sequentially-rebased chain -- that's the entire point of letting
   * `changes` take an array instead of forcing one dispatch per edit), and
   * simultaneous edits whose ranges overlap have no well-defined result.
   * Catching that here gives a precise, attributable reason before anything
   * is touched, rather than relying on whatever error (if any) CM6's own
   * internals throw for the same condition.
   */
  function validateEdits(edits, docLength) {
    if (!Array.isArray(edits) || edits.length === 0) {
      return { ok: false, reason: "edits must be a non-empty array." };
    }

    for (let i = 0; i < edits.length; i++) {
      const e = edits[i];
      if (!e || typeof e !== "object") {
        return { ok: false, reason: `edits[${i}] is not an object.` };
      }
      const { from, to, insert } = e;
      if (!Number.isInteger(from) || !Number.isInteger(to)) {
        return {
          ok: false,
          reason: `edits[${i}] has non-integer from/to (from=${JSON.stringify(from)}, to=${JSON.stringify(to)}).`,
        };
      }
      if (typeof insert !== "string") {
        return { ok: false, reason: `edits[${i}].insert must be a string.` };
      }
      if (from < 0 || to > docLength) {
        return {
          ok: false,
          reason: `edits[${i}] offset out of range [0, ${docLength}]: from=${from}, to=${to}.`,
        };
      }
      if (from > to) {
        return { ok: false, reason: `edits[${i}] has from (${from}) > to (${to}).` };
      }
    }

    // Sort a copy by position to check for overlaps; dispatch itself still
    // uses the caller's original array order (see applyEdits below).
    const byPosition = edits
      .map((e, originalIndex) => ({ from: e.from, to: e.to, originalIndex }))
      .sort((a, b) => a.from - b.from || a.to - b.to || a.originalIndex - b.originalIndex);
    for (let k = 1; k < byPosition.length; k++) {
      const prev = byPosition[k - 1];
      const cur = byPosition[k];
      if (cur.from < prev.to) {
        return {
          ok: false,
          reason:
            `edits[${prev.originalIndex}] (to=${prev.to}) and edits[${cur.originalIndex}] ` +
            `(from=${cur.from}) overlap.`,
        };
      }
    }

    return { ok: true };
  }

  /**
   * Computes, for each edit (indexed by its position in the ORIGINAL
   * `edits` array), the `[expectedFrom, expectedTo)` range its `insert`
   * text should occupy in the document AFTER dispatch.
   *
   * Why this needs its own offset-drift tracking: all edits in the batch
   * are specified against the SAME starting document (see validateEdits'
   * comment above) -- CM6's ChangeSet composes them internally and produces
   * one resulting document, but does not hand back each edit's individual
   * post-composition position. To verify edit N's text actually landed
   * where it should, we have to reproduce that composition ourselves: walk
   * the edits in ascending position order, and for each one add the
   * cumulative length delta (`insert.length - (to - from)`) of every edit
   * positioned before it. An edit with no edits before it lands exactly at
   * its own `from`; an edit after an earlier insertion lands shifted right
   * by that insertion's added length (or left, if an earlier edit deleted
   * more than it inserted).
   *
   * Ties (two edits at the identical `from`/`to`, e.g. two zero-width
   * insertions both at the same point) are broken by original array index,
   * matching the same tie-break validateEdits uses for its overlap scan, so
   * the two functions agree on ordering.
   */
  function computeExpectedRanges(edits) {
    const withIndex = edits.map((e, originalIndex) => ({ ...e, originalIndex }));
    withIndex.sort((a, b) => a.from - b.from || a.to - b.to || a.originalIndex - b.originalIndex);

    const expected = new Array(edits.length);
    let shift = 0;
    for (const e of withIndex) {
      const expectedFrom = e.from + shift;
      const expectedTo = expectedFrom + e.insert.length;
      expected[e.originalIndex] = { expectedFrom, expectedTo };
      shift += e.insert.length - (e.to - e.from);
    }
    return expected;
  }

  /**
   * Read-only lookup of one document line's exact character range + text,
   * via CM6's own `state.doc.line(lineNumber)` (1-indexed, matching both
   * CM6's own convention and unified-latex's `position.start.line` -- both
   * operate on the identical "\n"-joined text this extension already reads
   * via readEditorText()/outlineText() in content_script.js/engine, so the
   * two numbering schemes agree with no translation needed).
   *
   * This exists so the real node-editing flow (popup.js) can compute a
   * precise `{from, to}` character offset for a field inside a specific line
   * without reconstructing the document's line-start offsets itself from
   * `.cm-line` DOM text -- a second, independent source of truth that could
   * drift from CM6's actual state. Same "never trust anything but the live
   * document" principle applyEdits() below already applies at dispatch time;
   * this is the read-side equivalent, used just before computing the edit to
   * send there. Never dispatches, never mutates view.state -- purely a read.
   */
  function getLineInfo(lineNumber) {
    try {
      const { view, reason } = recoverEditorView();
      if (!view) return { found: false, reason };

      const doc = view.state.doc;
      if (!Number.isInteger(lineNumber) || lineNumber < 1 || lineNumber > doc.lines) {
        return {
          found: false,
          reason: `line ${lineNumber} is out of range [1, ${doc.lines}] for the live document.`,
        };
      }
      const line = doc.line(lineNumber);
      return { found: true, from: line.from, to: line.to, text: line.text };
    } catch (err) {
      return {
        found: false,
        reason: `Unexpected error reading line ${lineNumber}: ${err && err.message ? err.message : String(err)}`,
      };
    }
  }

  /**
   * The write-dispatch primitive. `edits` is an array of `{from, to, insert}`
   * (per write-back-spec.md's batched-not-per-call design). Never throws --
   * every failure path returns `{applied: false, reason}` instead, matching
   * the read path's never-raises convention.
   */
  function applyEdits(edits) {
    try {
      const { view, reason: recoverReason } = recoverEditorView();
      if (!view) {
        return { applied: false, reason: recoverReason };
      }

      // Validate against the CURRENT doc, right here, right before dispatch
      // -- never trust offsets computed from whenever the caller last read
      // the document. This is re-checked on every call; there is no cached
      // "last known length" anywhere in this file.
      const docLength = view.state.doc.length;
      const validation = validateEdits(edits, docLength);
      if (!validation.ok) {
        return { applied: false, reason: validation.reason };
      }

      const expectedRanges = computeExpectedRanges(edits);

      try {
        view.dispatch(
          view.state.update({
            changes: edits.map((e) => ({ from: e.from, to: e.to, insert: e.insert })),
          }),
        );
      } catch (dispatchErr) {
        // CM6 rejected the change set for a reason our own validation above
        // didn't independently catch. A transaction is applied atomically --
        // dispatch() either fully commits or throws before touching
        // view.state at all, so a throw here means the document is still
        // untouched, which is exactly the "never partially apply" contract.
        return {
          applied: false,
          reason: `CM6 rejected the edit batch: ${dispatchErr && dispatchErr.message ? dispatchErr.message : String(dispatchErr)}`,
        };
      }

      const newDoc = view.state.doc;
      const verified = edits.map((e, i) => {
        const { expectedFrom, expectedTo } = expectedRanges[i];
        if (expectedFrom < 0 || expectedTo > newDoc.length) return false;
        return newDoc.sliceString(expectedFrom, expectedTo) === e.insert;
      });

      return { applied: true, verified, newLength: newDoc.length };
    } catch (err) {
      return {
        applied: false,
        reason: `Unexpected error applying edits: ${err && err.message ? err.message : String(err)}`,
      };
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const msg = event.data;
    if (!msg || msg.source !== "meridian-latex-content") return;

    if (msg.type === REQUEST_TYPE) {
      const info = getDocInfo();
      window.postMessage(
        Object.assign({ source: RESPONSE_SOURCE, type: RESPONSE_TYPE }, info),
        window.location.origin,
      );
      return;
    }

    if (msg.type === APPLY_EDITS_REQUEST_TYPE) {
      const result = applyEdits(msg.edits);
      window.postMessage(
        Object.assign({ source: RESPONSE_SOURCE, type: APPLY_EDITS_RESPONSE_TYPE }, result),
        window.location.origin,
      );
      return;
    }

    if (msg.type === LINE_INFO_REQUEST_TYPE) {
      const info = getLineInfo(msg.lineNumber);
      window.postMessage(
        Object.assign({ source: RESPONSE_SOURCE, type: LINE_INFO_RESPONSE_TYPE }, info),
        window.location.origin,
      );
      return;
    }
  });
})();
