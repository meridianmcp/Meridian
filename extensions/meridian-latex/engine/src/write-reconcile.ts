// write.js's applyFieldEdit() submits an OT update and then awaits the
// server's otUpdateApplied confirmation -- but a timeout waiting for that
// confirmation, a dropped connection, or any other thrown error during the
// wait leaves the caller with NO way to know whether the edit actually
// reached the server. Live testing against both production Overleaf and a
// local Overleaf CE instance (2026-09-25) confirmed this is a real gap, not
// a theoretical one: an edit CAN land server-side even when the client
// never receives confirmation of it. A caller that blindly retries after
// that kind of failure risks double-applying an edit that already landed
// (e.g. a title edited twice, or a caption's old text getting clobbered a
// second time by an insert that's now anchored at the wrong offset). The
// only safe recovery is to re-fetch the document fresh, re-outline it, and
// figure out from the CURRENT content what actually happened -- which is
// this module's whole job. Pure and synchronous, no network I/O: the
// caller does the re-fetch/re-outline work and passes the results in.

import { fieldLineNumber, type OutlineNodeLike } from "./range-locate.js";

/** Reads the current value of `fieldName` off an outline node -- the same
 * per-field mapping write.js/range-locate.js use elsewhere (heading ->
 * `.title`, citation -> `.key`, table/figure/equation -> `.caption` or
 * `.label` depending on `fieldName`). Returns `undefined` for a
 * node/field combination the node doesn't carry rather than throwing.
 * Genuinely heterogeneous per field (a plain `unknown`, matching
 * OutlineNodeLike's own index-signature philosophy -- see its comment in
 * range-locate.ts) rather than a single narrower type. */
function currentFieldValue(node: OutlineNodeLike, fieldName: string): unknown {
  if (fieldName === "title") return node.title;
  if (fieldName === "key") return node.key;
  if (fieldName === "caption") return node.caption;
  if (fieldName === "label") return node.label;
  return undefined;
}

export interface ReconcileFieldEditParams {
  /** the doc's current lines, re-fetched via a fresh `joinDoc()` after the
   * failure. Not read directly by this function (the matching below relies
   * entirely on `freshNodes`' own recorded field values, which already
   * reflect this content) -- accepted here so callers can pass through the
   * same `{lines, nodes}` pair they re-fetched without repackaging it, and
   * so a future version of this function that needs raw line text (e.g. to
   * report surrounding context in an `ambiguous` reason) has it available
   * without a signature change. */
  freshLines: string[];
  /** outline.js's `outlineText()` output against `freshLines.join("\n")` --
   * the caller re-outlines; this function doesn't parse text itself. */
  freshNodes: OutlineNodeLike[];
  /** the ORIGINAL outline node the caller was trying to edit (has `.kind`,
   * `.line`, and the other fields outline.js produces for that kind). */
  target: OutlineNodeLike;
  /** matches write.js's own `computeFieldEditOps` `fieldName` parameter. */
  fieldName: "title" | "key" | "caption" | "label";
  /** the exact original value, as returned by `computeFieldEditOps`. */
  oldText: string;
  /** the exact text the original edit attempted to write. */
  newText: string;
}

export type ReconcileFieldEditResult =
  | { status: "applied"; node: OutlineNodeLike }
  | { status: "not_applied"; node: OutlineNodeLike }
  | { status: "ambiguous"; reason: string };

/**
 * Given a FRESH re-fetch of a document (re-joined and re-outlined AFTER an
 * `applyFieldEdit`/`session.applyUpdate` failure of unknown outcome),
 * figures out whether the original field edit actually landed, so the
 * caller never has to guess or blindly retry.
 *
 * Matching strategy: find every node in `freshNodes` sharing `target`'s
 * `.kind` whose OWN anchor line for `fieldName` (via `fieldLineNumber`,
 * the same function write.js/range-locate.js use for the forward write
 * path) equals `target`'s anchor line for that field. A node's own line
 * number is the best available anchor because single-field edits
 * (title/key/caption/label) never change which line the field lives on.
 * We deliberately do NOT try to positionally match `target` to one
 * specific candidate the way `locateHeadingRange`/`locateCitationRange`
 * do for a live write (there's no live line text here to count
 * occurrences against, only outline nodes) -- instead, if there are
 * multiple candidates at the same kind+line (rare, but the same real
 * sibling-disambiguation case range-locate.js itself flags), we ask a
 * looser but sufficient question: does ANY of them currently hold the new
 * text (the edit applied), or does ANY hold the old text (it didn't)?
 * This is a deliberate judgment call: a false positive would require two
 * same-kind nodes sharing one exact anchor line where one just happens to
 * coincidentally already contain the text being searched for -- possible
 * in principle, but no worse than the ambiguity two same-kind nodes on one
 * line already carries, and far more useful than refusing to reconcile
 * the common single-candidate case just because a rarer multi-candidate
 * one exists somewhere in the document.
 *
 * Returns one of:
 *   {status: "applied", node}      -- a candidate now holds `newText`.
 *   {status: "not_applied", node}  -- no candidate holds `newText`, but one
 *                                      still holds `oldText` (edit never landed).
 *   {status: "ambiguous", reason}  -- no candidate exists at all at that
 *                                      kind+line, or one exists but its
 *                                      value is neither `oldText` nor
 *                                      `newText`; `reason` is a clear,
 *                                      human-readable string, and for the
 *                                      latter case includes the actual
 *                                      current value(s) found.
 * Never throws for a well-formed input -- every unresolved case returns
 * `{status: "ambiguous", reason}` rather than guessing.
 */
export function reconcileFieldEdit({
  freshLines,
  freshNodes,
  target,
  fieldName,
  oldText,
  newText,
}: ReconcileFieldEditParams): ReconcileFieldEditResult {
  const targetLine = fieldLineNumber(target, fieldName);
  if (targetLine == null) {
    return {
      status: "ambiguous",
      reason:
        `Can't reconcile: the original target node has no line number for field "${fieldName}" ` +
        `(fieldLineNumber returned null), so there is no anchor to search the fresh outline against.`,
    };
  }

  const candidates = freshNodes.filter(
    (n) => n.kind === target.kind && fieldLineNumber(n, fieldName) === targetLine,
  );

  if (candidates.length === 0) {
    return {
      status: "ambiguous",
      reason:
        `No "${target.kind}" node found at line ${targetLine} for field "${fieldName}" in the fresh outline -- ` +
        `the node may have been deleted, or the surrounding document restructured since the original edit was attempted.`,
    };
  }

  const appliedMatch = candidates.find((n) => currentFieldValue(n, fieldName) === newText);
  if (appliedMatch) {
    return { status: "applied", node: appliedMatch };
  }

  const notAppliedMatch = candidates.find((n) => currentFieldValue(n, fieldName) === oldText);
  if (notAppliedMatch) {
    return { status: "not_applied", node: notAppliedMatch };
  }

  const foundValues = candidates.map((n) => `"${currentFieldValue(n, fieldName)}"`).join(", ");
  return {
    status: "ambiguous",
    reason:
      `Found ${candidates.length} "${target.kind}" node(s) at line ${targetLine} for field "${fieldName}" in the ` +
      `fresh outline, but none currently hold the expected old value ("${oldText}") or new value ("${newText}") -- ` +
      `current value(s) found: ${foundValues}.`,
  };
}
