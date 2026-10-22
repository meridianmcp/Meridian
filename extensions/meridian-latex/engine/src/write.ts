// The OT write primitive -- turns "edit this outline node's field to this
// new text" into real Overleaf OT ops and submits them via
// overleaf-ot-client.js, reusing range-locate.js's ported range-finding
// logic (the same occurrence-count/text-match safety nets the
// browser-extension write path already established) instead of the DOM.
// Item 2026-09-24, Adam's steer: focus on the OT path over the browser
// extension -- this is Layer 2 (the write primitive) of the LaTeX
// structural engine, sitting on top of Layer 1 (outline.js, already
// hardened) and the OT transport (overleaf-ot-client.js, already
// hardened + unit-tested against a mocked transport, not yet live-verified).

import type Database from "better-sqlite3";
import { fieldLineNumber, resolveFieldRange, type LineInfo, type OutlineNodeLike } from "./range-locate.js";
import { outlineText } from "./outline.js";
import { snapshotDoc } from "./local-snapshot.js";
import { reconcileFieldEdit, type ReconcileFieldEditResult } from "./write-reconcile.js";
import { claimNode, releaseClaims } from "./claims.js";
import { type OtOp } from "./overleaf-ot-client.js";

/** One editable field name this engine supports. */
export type FieldName = "title" | "key" | "caption" | "label";

/** `(err && err.message) || err`, but for an `unknown` catch-clause value --
 * same fallback shape claims.ts's own `describeError` uses. */
function describeError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return String(err);
}

/** `{from, to, text}` for 1-indexed `lineNumber` within `lines` (Overleaf's
 * own per-doc line array, as returned by `OverleafProjectSession.joinDoc`)
 * -- same semantics as CodeMirror 6's own `state.doc.line(n)`, which the
 * browser-extension write path's `lineInfo` already comes from (see
 * injected.js's getLineInfo): `to` excludes the line's own trailing
 * newline, and every line boundary in the flat doc is exactly one `\n`
 * character. Overleaf's real doc is CM6-backed server-side too, so this is
 * the correct offset convention, not an assumption unique to this file.
 * Returns `null` for an out-of-range line number rather than throwing,
 * mirroring injected.js's own `found: false` contract. */
export function lineInfoForLine(lines: string[], lineNumber: number): LineInfo | null {
  if (!Number.isInteger(lineNumber) || lineNumber < 1 || lineNumber > lines.length) return null;
  let from = 0;
  for (let i = 0; i < lineNumber - 1; i++) from += lines[i].length + 1;
  const text = lines[lineNumber - 1];
  return { from, to: from + text.length, text };
}

/** The full document text Overleaf's own doc model reduces to -- exactly
 * `lines.join("\n")`, matching `lineInfoForLine`'s own offset math. */
export function docText(lines: string[]): string {
  return lines.join("\n");
}

export type ComputeFieldEditOpsResult =
  | { ok: true; ops: OtOp[]; from: number; to: number; oldText: string }
  | { ok: false; reason: string };

/**
 * Computes the OT op(s) that replace one editable field (see
 * range-locate.js / popup.js's EDITABLE_FIELDS) with `newText`, against an
 * already-fetched doc (`lines`) and an already-computed outline (`nodes`,
 * matching that same content) -- pure and synchronous, no network I/O, so
 * it's testable without a live session. Returns
 * `{ok:true, ops, from, to, oldText}` or `{ok:false, reason}` (never
 * throws) -- the exact same safety nets popup.js's write-dispatch path
 * already established apply here identically, since this calls the same
 * ported range-locate.js logic.
 *
 * A combined replace is expressed as delete-then-insert AT THE SAME
 * position (`range.from`) rather than two independently-addressed ops --
 * this sidesteps any ambiguity in whether Overleaf's OT server applies a
 * batched op list's positions sequentially (each op's `p` relative to the
 * doc state after prior ops in the SAME update already applied) or against
 * the pristine pre-update doc, since both interpretations place `newText`
 * at the exact same final location when both ops share one anchor
 * position. Not yet live-verified against a real multi-op batch -- same
 * flagged gap overleaf-ot-client.js's own applyUpdate() doc comment
 * already notes for version incrementing on a multi-op update.
 *
 * @param target the outline node to edit (must be present in `nodes`)
 */
export function computeFieldEditOps(
  lines: string[],
  nodes: OutlineNodeLike[],
  target: OutlineNodeLike,
  fieldName: FieldName,
  newText: string,
): ComputeFieldEditOpsResult {
  const lineNumber = fieldLineNumber(target, fieldName);
  if (lineNumber == null) {
    return {
      ok: false,
      reason:
        target.kind === "heading" || target.kind === "citation"
          ? "This node has no line number in the current outline data."
          : `This node has no ${fieldName} in the current outline data.`,
    };
  }
  const lineInfo = lineInfoForLine(lines, lineNumber);
  if (!lineInfo) {
    return { ok: false, reason: `Line ${lineNumber} is out of range for this document (${lines.length} lines).` };
  }
  const range = resolveFieldRange(lineInfo, target, nodes, fieldName);
  if (!range.ok) return range;

  const oldText = docText(lines).slice(range.from, range.to);
  const ops: OtOp[] =
    oldText.length > 0
      ? [
          { d: oldText, p: range.from },
          { i: newText, p: range.from },
        ]
      : [{ i: newText, p: range.from }];
  return { ok: true, ops, from: range.from, to: range.to, oldText };
}

/** The minimal shape this module needs from a live Overleaf session --
 * real `OverleafProjectSession` (overleaf-ot-client.ts) satisfies this.
 * `projectId` is optional here only to accommodate this file's own test
 * suite's minimal fake sessions that never exercise the claims-aware path
 * (the only path that reads it). */
export interface WriteSession {
  projectId?: string;
  joinDoc(docId: string): Promise<{ lines: string[]; version: number }>;
  applyUpdate(docId: string, version: number, op: OtOp[], trackChanges?: boolean): Promise<{ version: number }>;
}

/** Opt-in claim coordination -- see `applyFieldEdit`'s own doc comment. */
export interface ClaimsOption {
  db: Database.Database;
  holder_token: string;
}

export type ApplyFieldEditResult =
  | {
      ok: true;
      version: number;
      from: number;
      to: number;
      oldText: string;
      newText: string;
      snapshotPath: string | null;
      reconciled: false;
    }
  | {
      ok: boolean;
      reconciled: true;
      reconcileStatus: ReconcileFieldEditResult["status"];
      reconcileReason?: string;
      version: number;
      from: number;
      to: number;
      oldText: string;
      newText: string;
      snapshotPath: string | null;
      originalError: string;
    }
  | {
      ok: false;
      reason: string;
      claimedBy?: string;
      claimReason?: string;
      /** never set on this variant (a rejected claim / an unlocatable
       * field never reaches the write path at all) -- both declared here
       * (always `undefined`) purely so callers can read `.reconciled`/
       * `.version` off this union without narrowing first, matching how
       * this file's own test suite checks them. */
      reconciled?: undefined;
      version?: undefined;
    };

/**
 * High-level, one-shot convenience: join `docId` fresh (always the
 * authoritative current version -- see `OverleafProjectSession.joinDoc`'s
 * own "never trust a stale read" comment), compute this field edit's ops
 * against that just-fetched content, and submit it.
 *
 * ROBUSTNESS (2026-09-25, Adam's steer: "bare minimum... robust and solid
 * on Overleaf, since most people use this via Overleaf" -- live testing
 * that day, against both production Overleaf and a local Overleaf CE
 * instance, confirmed real gaps this closes):
 *
 * 1. PRE-WRITE SNAPSHOT: before submitting, the doc's current full text is
 *    snapshotted locally (see local-snapshot.js) as a one-directional
 *    safety net -- never read back by this engine, never synced to
 *    Overleaf. A snapshot failure (a genuine disk/permission error) is
 *    caught and IGNORED here, deliberately: the snapshot is insurance, not
 *    a gate, and must never block a real edit the user asked for. The
 *    returned `snapshotPath` is `null` if the snapshot itself failed.
 * 2. RECONCILE-ON-FAILURE: if `session.applyUpdate` throws for ANY reason
 *    (a timeout waiting for confirmation, a dropped connection, an
 *    ack-level rejection), this does NOT just propagate an opaque error.
 *    Live testing confirmed an edit can genuinely land server-side even
 *    when the client never receives confirmation of it -- so blindly
 *    reporting "failed" (and inviting a caller to retry) risks a *second*
 *    write actually being the one that double-applies. Instead, a fresh
 *    `joinDoc` + re-outline + `reconcileFieldEdit` (see write-reconcile.js)
 *    determines what ACTUALLY happened: `{ok:true, reconciled:true,
 *    reconcileStatus:"applied", ...}` if the edit is confirmed present
 *    despite the failure, `{ok:false, reconciled:true,
 *    reconcileStatus:"not_applied", ...}` if it's confirmed NOT present
 *    (genuinely safe to retry), or `{ok:false, reconciled:true,
 *    reconcileStatus:"ambiguous", reconcileReason, ...}` if neither can be
 *    determined (a human should look, not an automatic retry). If the
 *    reconciliation attempt ITSELF fails (e.g. the connection is still
 *    down), the ORIGINAL error from `applyUpdate` is rethrown -- this
 *    function only ever resolves to a reconciled outcome when it
 *    genuinely could reconcile one.
 *
 * Returns `{ok:true, version, from, to, oldText, newText, snapshotPath,
 * reconciled:false}` on a clean write, a reconciled outcome shaped as
 * above on a recovered failure, or `{ok:false, reason}` if the edit
 * couldn't be safely located in the first place (never throws for THAT
 * case, and no network write is ever attempted for it, so no snapshot is
 * taken either).
 *
 * Callers doing MULTIPLE edits in one sitting should instead call
 * `session.joinDoc()` / `computeFieldEditOps()` / `session.applyUpdate()`
 * themselves and reuse one fetched doc + one connection -- this wrapper is
 * for the common single-edit case (CLI use, a one-off scripted fix) where
 * paying a fresh `joinDoc()` round-trip per edit is the safer default over
 * silently reusing a possibly-stale version.
 *
 * OPTIONAL CLAIM COORDINATION (2026-09-25): passing `claims` as
 * `{db, holder_token}` (an already-open claims.js/store.js `openStore()`
 * handle plus a caller-chosen holder_token string) makes this claim-aware --
 * see claims.js for the underlying conflict rules. This is opt-in: omitting
 * `claims` (the default) leaves behavior byte-for-byte identical to before
 * this option existed -- no claims.js call of any kind, so a caller with no
 * shared claims db (or that doesn't care about cross-caller coordination for
 * this particular edit) pays no cost.
 *
 * When `claims` IS provided:
 *   1. Before doing anything else -- no `joinDoc`, no `applyUpdate` -- this
 *      calls `claimNode(db, {project_id: session.projectId, node_id:
 *      target.id, holder_token})`. If the node is already claimed by a
 *      DIFFERENT holder (or the claim is otherwise rejected, e.g. a live
 *      whole-document lease held by someone else), this returns
 *      `{ok: false, reason: "node already claimed", claimedBy, claimReason}`
 *      immediately -- `session.applyUpdate` is never reached, so a node
 *      someone else is actively editing is never silently clobbered.
 *   2. On a successful claim (fresh or already held by this same
 *      holder_token), the existing write flow below runs completely
 *      unchanged -- same snapshot, same `applyUpdate`, same
 *      reconcile-on-failure logic.
 *   3. The claim is released (`releaseClaims`) in a finally block covering
 *      every exit from that point on -- a clean success, a reconciled
 *      outcome (applied/not_applied/ambiguous), a pre-write locate failure,
 *      or a rethrown original error when reconciliation itself can't even
 *      fetch -- so the claim never lingers past this one call, however it
 *      ends.
 *
 * @param nodes a fresh outline (see outline.js's outlineText) matched against this doc's CURRENT content
 * @param target the outline node to edit (must be present in `nodes`)
 * @param snapshotDir overrides local-snapshot.js's default `~/.meridian-latex/snapshots` base directory -- for tests only, so the real config directory is never touched by anything other than a genuine live write
 * @param claims opt-in claim coordination -- see above. Omit for today's unclaimed behavior.
 */
export async function applyFieldEdit(
  session: WriteSession,
  docId: string,
  nodes: OutlineNodeLike[],
  target: OutlineNodeLike,
  fieldName: FieldName,
  newText: string,
  trackChanges = false,
  snapshotDir: string | undefined = undefined,
  claims: ClaimsOption | undefined = undefined,
): Promise<ApplyFieldEditResult> {
  const { db, holder_token: holderToken } = claims || ({} as Partial<ClaimsOption>);
  const claimsEnabled = Boolean(db && holderToken);

  if (claimsEnabled) {
    const claimResult = claimNode(db as Database.Database, {
      project_id: session.projectId,
      node_id: target.id,
      holder_token: holderToken,
    });
    if (!claimResult.claimed) {
      return {
        ok: false,
        reason: "node already claimed",
        claimedBy: claimResult.holder_token_of_conflict,
        claimReason: claimResult.reason,
      };
    }
  }

  try {
    const { lines, version } = await session.joinDoc(docId);
    const computed = computeFieldEditOps(lines, nodes, target, fieldName, newText);
    if (!computed.ok) return computed;

    let snapshotPath: string | null = null;
    try {
      // session.projectId is optional only to accommodate this file's own
      // test suite's minimal fake sessions (see WriteSession's own comment);
      // a real caller's session always carries a real projectId. If it's
      // genuinely missing, snapshotDoc throws (a real path-join failure),
      // which the catch below already treats as "insurance, not a gate".
      snapshotPath = snapshotDoc({ projectId: session.projectId as string, docId, lines, dir: snapshotDir });
    } catch {
      // Insurance, not a gate -- a snapshot failure must never block the
      // real edit the user asked for. snapshotPath stays null.
    }

    try {
      const result = await session.applyUpdate(docId, version, computed.ops, trackChanges);
      return {
        ok: true,
        version: result.version,
        from: computed.from,
        to: computed.to,
        oldText: computed.oldText,
        newText,
        snapshotPath,
        reconciled: false,
      };
    } catch (err) {
      let fresh: { lines: string[]; version: number };
      try {
        fresh = await session.joinDoc(docId);
      } catch {
        throw err; // Reconciliation itself couldn't even fetch -- surface the ORIGINAL failure.
      }
      // outlineText() returns OutlineNode[] (outline.ts's own precise node
      // type); reconcileFieldEdit expects the looser, index-signature-bearing
      // OutlineNodeLike[] range-locate.ts declares for testability (see its
      // own comment) -- structurally compatible field-for-field, just
      // missing that index signature, so a direct cast is safe here.
      const freshNodes = outlineText(fresh.lines.join("\n")) as OutlineNodeLike[];
      const reconciled = reconcileFieldEdit({
        freshLines: fresh.lines,
        freshNodes,
        target,
        fieldName,
        oldText: computed.oldText,
        newText,
      });
      return {
        ok: reconciled.status === "applied",
        reconciled: true,
        reconcileStatus: reconciled.status,
        reconcileReason: reconciled.status === "ambiguous" ? reconciled.reason : undefined,
        version: fresh.version,
        from: computed.from,
        to: computed.to,
        oldText: computed.oldText,
        newText,
        snapshotPath,
        originalError: describeError(err),
      };
    }
  } finally {
    if (claimsEnabled) {
      releaseClaims(db as Database.Database, { project_id: session.projectId, holder_token: holderToken, node_id: target.id });
    }
  }
}
