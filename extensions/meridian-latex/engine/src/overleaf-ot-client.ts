// Original client for Overleaf's real-time collaborative-editing protocol
// (project join / doc join / OT update submission / track-changes), built
// on the Socket.IO 0.9.x transport in ./socketio09/. See the pinned
// Meridian decision "Overleaf real-time OT protocol: full reverse-
// engineered spec..." (project meridian-build) for the full research trail
// this implementation is based on -- every field/event name below is a
// confirmed, twice-verified fact traced from Overleaf's own official
// open-source repo (github.com/overleaf/overleaf), read for PROTOCOL FACTS
// only. This file's code is 100% original, not copied or adapted from
// Overleaf's or any third party's (e.g. netique/overleaf-mcp, AGPL-3.0)
// implementation.
//
// Scope (the write-path MVP -- see sprint item 50dbc188):
//   - connectToProject(): handshake + joinProject (automatic server-side)
//   - .joinDoc(docId): fresh doc content + version + track-changes state
//   - .applyUpdate(docId, {version, op, trackChanges}): submit an edit,
//     wait for BOTH the immediate ack (request accepted/queued) AND the
//     asynchronous otUpdateApplied broadcast (the update was actually
//     applied) before resolving -- see _waitForApplied()'s own comment for
//     why a bare ack is not sufficient confirmation.
//   - otUpdateError handling: never silently retried -- surfaced to the
//     caller, who must rejoin the doc (a fresh .joinDoc() call) for a
//     correct current version before trying again.
//
// NOT in scope for the MVP: compile/comments (HTTP-based, confirmed lower
// priority in the research decision), XHR-polling fallback (WebSocket only
// -- reasonable for a server-side/agent client with no corporate-proxy
// constraint a browser client would have), automatic reconnection (see
// Socket09Client's own header comment -- a dropped connection is always
// surfaced to the caller, never silently retried).

import type { EventEmitter } from "node:events";
import { Socket09Client } from "./socketio09/client.js";
import { unwrapRootFolder, resolveDocIdByPath, type FileTreeFolder, type ResolveDocIdResult } from "./project-tree.js";

const DEFAULT_APPLIED_TIMEOUT_MS = 15_000;

/** One OT op, as Overleaf's real-time protocol expects it: an insert, a
 * delete, or a (never yet produced by write.js, but part of the confirmed
 * wire shape) tracked-comment op. Shared with write.js's own
 * computeFieldEditOps, which only ever produces the `i`/`d` variants. */
export type OtOp = { i: string; p: number } | { d: string; p: number } | { c: string; p: number; t: string };

/** The minimal shape this module needs from a transport (real `Socket09Client`
 * or this file's own test suite's `FakeTransport`) -- a real `EventEmitter`
 * (for "event"/"error"/"disconnect") plus connect/close/emitWithAck. */
export interface TransportLike extends EventEmitter {
  connect(): Promise<void>;
  close(): void;
  emitWithAck(name: string, args?: unknown[]): Promise<unknown[]>;
}

/** The options every real call site (`connectToProject`) constructs a
 * transport with -- all four fields, always present. */
export interface TransportConstructorOptions {
  httpBaseUrl: string;
  wsBaseUrl: string;
  query: string;
  headers: Record<string, string>;
}

/** The minimal shape of a transport *constructor* this module needs --
 * real `Socket09Client` satisfies this, and so does this file's own test
 * suite's `FakeTransport`/its subclasses. */
export interface TransportConstructor {
  new (options: TransportConstructorOptions): TransportLike;
}

/** Raised when the server responds to `joinDoc`/`applyOtUpdate` with an
 * error as the first ack argument (Overleaf's own callback-based API
 * convention: `(error, ...results)`), OR when an `otUpdateError` event
 * arrives for an update this client submitted. Callers should treat this
 * as "assume nothing about the document's current state" and re-`joinDoc`
 * before trying again -- never blind-retry the same version number, per
 * the confirmed protocol (a stale version can be legitimately transformed
 * for up to 80 ops of drift, but re-deriving the correct current version
 * via a fresh joinDoc is always safe and never wrong). */
export class OverleafOtError extends Error {
  cause?: unknown;

  constructor(message: string, options: { cause?: unknown } = {}) {
    super(message);
    this.name = "OverleafOtError";
    if (options.cause !== undefined) this.cause = options.cause;
  }
}

/** `connectToProject`'s own options. `projectId`/`cookie` are typed
 * optional here (rather than required) so this file's own test suite can
 * exercise the RUNTIME required-field checks below (`if (!projectId) throw
 * ...`) without those calls being rejected at compile time first -- the
 * runtime check is the real contract, matching this codebase's established
 * convention (see claims.ts's `unknown`-typed, runtime-validated params)
 * of validating at the boundary rather than only at the type level. */
export interface ConnectToProjectOptions {
  projectId?: string;
  /** e.g. "https://www.overleaf.com" */
  httpBaseUrl: string;
  /** e.g. "wss://www.overleaf.com" */
  wsBaseUrl: string;
  /** the full `Cookie` header value for an authenticated session -- THIS
   * CLIENT NEVER CAPTURES OR STORES A COOKIE ITSELF, see the sprint item's
   * own notes on the safe `login`-command pattern for how a caller should
   * obtain one */
  cookie?: string;
  /** injectable for testing */
  Socket09ClientImpl?: TransportConstructor;
  appliedTimeoutMs?: number;
}

/** The slice of a real `joinProjectResponse` payload this module reads. */
interface JoinProjectResponsePayload {
  publicId?: string;
  project?: {
    rootFolder?: FileTreeFolder[];
    trackChangesState?: Record<string, boolean>;
  };
}

/**
 * Connects to one Overleaf project's real-time session. Resolves once the
 * server's `joinProjectResponse` has arrived (project-level join is
 * automatic server-side per the confirmed protocol -- no client action
 * needed beyond connecting).
 */
export async function connectToProject({
  projectId,
  httpBaseUrl,
  wsBaseUrl,
  cookie,
  Socket09ClientImpl = Socket09Client,
  appliedTimeoutMs = DEFAULT_APPLIED_TIMEOUT_MS,
}: ConnectToProjectOptions): Promise<OverleafProjectSession> {
  if (!projectId) throw new OverleafOtError("connectToProject: projectId is required");
  if (!cookie) throw new OverleafOtError("connectToProject: cookie is required");

  const transport = new Socket09ClientImpl({
    httpBaseUrl,
    wsBaseUrl,
    query: `projectId=${encodeURIComponent(projectId)}`,
    headers: { Cookie: cookie },
  });

  const session = new OverleafProjectSession({ projectId, transport, appliedTimeoutMs });

  // joinProjectResponse arrives unprompted right after connect (confirmed:
  // WebsocketController auto-calls joinProject server-side) -- wait for it
  // here so a caller never has to know that detail themselves.
  let joinProjectTimer: NodeJS.Timeout | undefined;
  const onJoinProjectEvent = function onEvent(e: { name: string; args: unknown[] }): void {
    if (e.name !== "joinProjectResponse") return;
    clearTimeout(joinProjectTimer);
    transport.off("event", onJoinProjectEvent);
    resolveJoinProject((e.args[0] as JoinProjectResponsePayload) || {});
  };
  let resolveJoinProject: (response: JoinProjectResponsePayload) => void;
  const joinProjectPromise = new Promise<JoinProjectResponsePayload>((resolve, reject) => {
    resolveJoinProject = resolve;
    joinProjectTimer = setTimeout(
      () => reject(new OverleafOtError("connectToProject: timed out waiting for joinProjectResponse")),
      appliedTimeoutMs,
    );
    transport.on("event", onJoinProjectEvent);
  });

  try {
    await transport.connect();
  } catch (err) {
    // Real bug found 2026-09-18 via independent code review: without this
    // cleanup, a connect() failure left joinProjectPromise's own timer
    // running -- it would fire ~appliedTimeoutMs later and reject an
    // already-abandoned promise nothing is awaiting anymore (this function
    // has already thrown and returned control to the caller by then),
    // surfacing as a delayed unhandled promise rejection.
    clearTimeout(joinProjectTimer);
    transport.off("event", onJoinProjectEvent);
    throw err;
  }
  const joinProjectResponse = await joinProjectPromise;
  session._applyJoinProjectResponse(joinProjectResponse);
  return session;
}

/** The options `OverleafProjectSession`'s constructor takes -- always
 * constructed either by `connectToProject` above or directly by this
 * file's own test suite, both of which always supply all three fields. */
export interface OverleafProjectSessionOptions {
  projectId: string;
  transport: TransportLike;
  appliedTimeoutMs: number;
}

interface AppliedWaiter {
  waiterId: symbol;
  expectedVersion: number;
  timer: NodeJS.Timeout;
  resolve: () => void;
  reject: (err: Error) => void;
}

/** The confirmed wire shape of an `otUpdateApplied` broadcast's payload. */
interface OtUpdateAppliedPayload {
  doc: string;
  v: number;
}

/** The confirmed wire shape of an `otUpdateError` broadcast's second
 * argument. */
interface OtUpdateErrorPayload {
  doc_id: string;
}

/**
 * One live Overleaf project connection. Not constructed directly -- use
 * `connectToProject()`.
 */
export class OverleafProjectSession {
  projectId: string;
  transport: TransportLike;
  appliedTimeoutMs: number;
  publicId: string | null;
  /** `{userId: boolean}` map, `__guests__` for anonymous users -- see
   * `trackChangesOnForUser()`. Refreshed live by a `toggle-track-changes`
   * push (see _attachEventRouting) as well as at join time. */
  trackChangesState: Record<string, boolean>;
  /** The project's file tree root (already unwrapped from the confirmed
   * `project.rootFolder` one-element-array shape -- see project-tree.js's
   * own header comment for the full, cited confirmation against
   * Overleaf's real frontend source), or `null` before the first
   * joinProjectResponse arrives. Use `resolveDocId()` below rather than
   * walking this directly, unless you need the raw tree. */
  rootFolder: FileTreeFolder | null;
  /** docId -> array of pending applyUpdate() waiters. Not marked TS-private
   * -- this file's own test suite inspects it directly to assert there is
   * no unbounded growth (see the two "_appliedWaiters does not accumulate
   * empty entries" tests). */
  _appliedWaiters: Map<string, AppliedWaiter[]>;
  _lastTransportError?: Error;

  constructor({ projectId, transport, appliedTimeoutMs }: OverleafProjectSessionOptions) {
    this.projectId = projectId;
    this.transport = transport;
    this.appliedTimeoutMs = appliedTimeoutMs;
    this.publicId = null;
    this.trackChangesState = {};
    this.rootFolder = null;
    this._appliedWaiters = new Map();
    // Wired unconditionally here, not as a separate opt-in step a caller
    // could forget -- a constructed session must ALWAYS route
    // otUpdateApplied/otUpdateError/toggle-track-changes correctly, since
    // applyUpdate()'s own safety (never resolving on the ack alone) depends
    // on it entirely.
    this._attachEventRouting();
  }

  _applyJoinProjectResponse(response: JoinProjectResponsePayload): void {
    this.publicId = response.publicId || null;
    this.trackChangesState = (response.project && response.project.trackChangesState) || {};
    this.rootFolder = unwrapRootFolder(response.project);
  }

  /**
   * Resolves a project-relative path (e.g. "chapters/intro.tex") to the
   * docId `joinDoc()`/`write.js`'s `applyFieldEdit()` expect, against the
   * file tree captured from this session's own joinProjectResponse. See
   * project-tree.js's `resolveDocIdByPath` for the confirmed shape this
   * walks and its `{ok, ...}` return convention.
   */
  resolveDocId(path: string): ResolveDocIdResult {
    return resolveDocIdByPath(this.rootFolder, path);
  }

  _attachEventRouting(): void {
    this.transport.on("event", (e: { name: string; args: unknown[] }): void => {
      if (e.name === "toggle-track-changes") {
        this.trackChangesState = (e.args[0] as Record<string, boolean>) || {};
        return;
      }
      if (e.name === "otUpdateApplied" || e.name === "otUpdateError") {
        this._resolveAppliedWaiters(e.name, e.args);
      }
    });

    // Real bugs found 2026-09-18 via independent code review:
    // (1) Socket09Client documents "error" as part of its public event
    //     contract (a raw WebSocket error, a malformed server "error"
    //     packet, an unrecognized packet) but nothing here ever listened
    //     for it -- Node's EventEmitter throws SYNCHRONOUSLY (can crash the
    //     whole process) when "error" is emitted with zero listeners. This
    //     was a real, live crash risk on exactly the first real transport
    //     error Adam's own live verification was likely to hit.
    // (2) This session never listened for the transport's "disconnect"
    //     event either, so a genuine connection drop mid-write left any
    //     pending applyUpdate() waiter to fail LATE (only once its own
    //     appliedTimeoutMs elapsed) with a misleading "timed out waiting
    //     for otUpdateApplied" message, instead of an immediate, accurate
    //     one naming the real cause.
    // Both fixed the same way: reject every pending waiter across every
    // doc immediately with a clear, accurate reason (there's no single doc
    // a transport-level failure is scoped to, unlike otUpdateError).
    this.transport.on("error", (err: Error): void => {
      this._lastTransportError = err;
      this._rejectAllAppliedWaiters(new OverleafOtError(`transport error: ${err.message}`, { cause: err }));
    });
    this.transport.on("disconnect", ({ code, reason }: { code: number; reason: string }): void => {
      this._rejectAllAppliedWaiters(
        new OverleafOtError(
          `connection closed while waiting for otUpdateApplied (code ${code}${reason ? `, reason: ${reason}` : ""})`,
        ),
      );
    });
  }

  /** Whether Track Changes is currently on for a given user id (default:
   * this session's OWN user, if the server told us via publicId -- but
   * publicId is a per-CONNECTION id, not a user id, so callers that know
   * their own Overleaf user id should pass it explicitly; `__guests__` is
   * the documented fallback key for an anonymous/guest session). */
  trackChangesOnForUser(userId?: string): boolean {
    if (userId && userId in this.trackChangesState) return Boolean(this.trackChangesState[userId]);
    return Boolean(this.trackChangesState.__guests__);
  }

  /**
   * Joins (or re-joins) a document, always requesting from version 0 --
   * i.e. always the FULL current content, never a delta -- since this
   * client's callers (the outline/matching engine) need the complete text
   * every time, matching the "never trust a stale read" discipline the
   * existing browser-extension write path already established.
   */
  async joinDoc(
    docId: string,
  ): Promise<{ lines: string[]; version: number; ranges: Record<string, unknown>; docType: string }> {
    const [error, lines, version, , ranges, docType] = (await this.transport.emitWithAck("joinDoc", [
      docId,
      0,
      {},
    ])) as [unknown, string[] | undefined, number, unknown, Record<string, unknown> | undefined, string];
    if (error) {
      throw new OverleafOtError(`joinDoc failed for ${docId}: ${JSON.stringify(error)}`, { cause: error });
    }
    return { lines: lines || [], version, ranges: ranges || {}, docType };
  }

  /**
   * Submits one OT update (one or more ops applied as a single version
   * transition) and resolves only once BOTH:
   *   1. the immediate ack confirms the server accepted/queued it (a bare
   *      `(error)` callback per Overleaf's own WebsocketController --
   *      NOT confirmation it was actually applied, just that the request
   *      itself was well-formed and the client is allowed to write here), AND
   *   2. the asynchronous `otUpdateApplied` broadcast for this doc arrives.
   *      LIVE-VERIFIED 2026-09-25 (real disposable Overleaf project,
   *      ooxml-graph-paper-overleaf): the broadcast's `v` field echoes the
   *      SUBMITTED base version (`update.v`, i.e. the `version` argument
   *      below), NOT `version + 1` as originally assumed from static
   *      protocol research -- confirmed by direct reproduction: submitted
   *      `{v: 2, op: [...]}`, received `otUpdateApplied` with `v: 2`, then
   *      an independent fresh `joinDoc()` afterward read back `version: 3`.
   *      So the resulting version genuinely IS `version + 1` (one
   *      `applyOtUpdate` call is one version transition, confirmed), but
   *      the broadcast is a same-version ack-of-application, not a
   *      new-version announcement -- `_waitForApplied` below matches on
   *      the SUBMITTED version and computes the resulting version as
   *      `matchedVersion + 1` itself, rather than trusting a `+1` to
   *      arrive in the payload. (Still not live-verified against a real
   *      multi-op batch specifically -- both live tests so far submitted
   *      exactly one delete+insert pair as a single update, which is the
   *      only shape write.js's computeFieldEditOps ever produces.)
   *
   * Rejects with OverleafOtError on either an ack-level error OR an
   * `otUpdateError` broadcast -- a caller must NEVER blind-retry the same
   * version after either; re-`joinDoc()` for the true current state first.
   */
  async applyUpdate(docId: string, version: number, op: OtOp[], trackChanges = false): Promise<{ version: number }> {
    const update: { v: number; op: OtOp[]; meta?: { tc: string } } = { v: version, op };
    if (trackChanges) update.meta = { tc: generateTcIdSeed() };

    const appliedPromise = this._waitForApplied(docId, version);

    // Real bug found 2026-09-18 via independent code review: _cancelAppliedWait
    // was only ever called when the ack ITSELF resolved with a truthy error
    // value -- not when emitWithAck's own promise REJECTS (ack timeout, or
    // the socket closing before any ack arrives -- see emitWithAck's own
    // doc comment). In that case control left this function via the `await`
    // below without ever reaching the `if (ackError)` check, leaving the
    // otUpdateApplied waiter registered above dangling -- its own timer
    // would fire ~appliedTimeoutMs later and reject an abandoned promise
    // nothing is awaiting anymore, the exact unhandled-rejection class of
    // bug _cancelAppliedWait's OWN timer-clearing was already built to
    // prevent, just reached via a different, previously-uncovered path.
    let ackError: unknown;
    try {
      [ackError] = await this.transport.emitWithAck("applyOtUpdate", [docId, update]);
    } catch (err) {
      this._cancelAppliedWait(docId, appliedPromise.waiterId);
      throw err;
    }
    if (ackError) {
      this._cancelAppliedWait(docId, appliedPromise.waiterId);
      throw new OverleafOtError(`applyOtUpdate rejected for ${docId}: ${JSON.stringify(ackError)}`, {
        cause: ackError,
      });
    }

    return appliedPromise.promise;
  }

  /** Registers a waiter for the NEXT `otUpdateApplied` matching `docId` and
   * `expectedVersion` (the SUBMITTED base version -- see applyUpdate's own
   * doc comment for the live-verified reason this is the base version, not
   * base+1), or the NEXT `otUpdateError` for `docId` (treated as this
   * update's own failure -- the protocol gives no per-update correlation id
   * on the error path, so any error for this doc while we have an
   * outstanding write is attributed to it; a real concurrent OTHER-client
   * error landing in this exact window is a known, accepted imprecision for
   * this MVP, flagged for the live-verification pass). Resolves with
   * `expectedVersion + 1` (the true resulting version), not the raw
   * broadcast value. */
  _waitForApplied(docId: string, expectedVersion: number): { promise: Promise<{ version: number }>; waiterId: symbol } {
    const waiterId = Symbol("applied-waiter");
    let resolveFn: () => void;
    let rejectFn: (err: Error) => void;
    const promise = new Promise<{ version: number }>((resolve, reject) => {
      resolveFn = () => resolve({ version: expectedVersion + 1 });
      rejectFn = reject;
    });

    const timer = setTimeout(() => {
      this._cancelAppliedWait(docId, waiterId);
      rejectFn(
        new OverleafOtError(
          `applyUpdate: timed out waiting for otUpdateApplied on ${docId} (expected version ${expectedVersion})`,
        ),
      );
    }, this.appliedTimeoutMs);

    const waiters = this._appliedWaiters.get(docId) || [];
    waiters.push({
      waiterId,
      expectedVersion,
      timer,
      resolve: () => {
        clearTimeout(timer);
        // The resulting version is the submitted base version + 1 -- see
        // applyUpdate's own doc comment; _resolveAppliedWaiters below
        // matches on the broadcast's `v` (the base version) but does not
        // pass it through directly, since it is not the new version.
        resolveFn();
      },
      reject: (err: Error) => {
        clearTimeout(timer);
        rejectFn(err);
      },
    });
    this._appliedWaiters.set(docId, waiters);

    return { promise, waiterId };
  }

  /** Removes a waiter WITHOUT settling its promise -- used only when the
   * caller (applyUpdate, on an ack-level error) is about to reject via a
   * different path and the promise this waiter guards will never be
   * awaited at all. Must clear the waiter's own timer here too: leaving it
   * running would fire its timeout rejection later against an
   * already-abandoned, unawaited promise -- a real bug hit while testing
   * this file (surfaced as an unhandled rejection in the test runner,
   * ~appliedTimeoutMs after the test itself had already finished and
   * moved on). */
  _cancelAppliedWait(docId: string, waiterId: symbol): void {
    const waiters = this._appliedWaiters.get(docId);
    if (!waiters) return;
    const waiter = waiters.find((w) => w.waiterId === waiterId);
    if (waiter) clearTimeout(waiter.timer);
    const remaining = waiters.filter((w) => w.waiterId !== waiterId);
    // Real bug found 2026-09-18 via independent code review: this used to
    // `.set(docId, [])` rather than deleting the key once a doc's waiter
    // list empties -- a long session touching many distinct docs would
    // accumulate one permanent, never-cleaned Map entry per docId ever
    // written to, each holding a trivial but real empty array (unbounded
    // growth over the session's lifetime).
    if (remaining.length === 0) this._appliedWaiters.delete(docId);
    else this._appliedWaiters.set(docId, remaining);
  }

  _resolveAppliedWaiters(eventName: string, args: unknown[]): void {
    if (eventName === "otUpdateApplied") {
      const payload = args[0] as OtUpdateAppliedPayload | undefined;
      const docId = payload && payload.doc;
      const version = payload && payload.v;
      // docId may be undefined for a malformed/unexpected payload -- the
      // cast below is defensive only: a Map<string, ...>'s .get() with an
      // undefined key at runtime simply finds nothing (same as today's
      // untyped behavior), it never throws.
      const waiters = this._appliedWaiters.get(docId as string);
      if (!waiters || waiters.length === 0) return;
      const matchIndex = waiters.findIndex((w) => w.expectedVersion === version);
      if (matchIndex === -1) return; // someone else's update on this doc -- not ours, ignore
      const [waiter] = waiters.splice(matchIndex, 1);
      // Same unbounded-growth fix as _cancelAppliedWait above.
      if (waiters.length === 0) this._appliedWaiters.delete(docId as string);
      else this._appliedWaiters.set(docId as string, waiters);
      waiter.resolve();
      return;
    }
    if (eventName === "otUpdateError") {
      const [errorReason, payloadRaw] = args;
      const payload = payloadRaw as OtUpdateErrorPayload | undefined;
      const docId = payload && payload.doc_id;
      const waiters = this._appliedWaiters.get(docId as string);
      if (!waiters || waiters.length === 0) return;
      this._appliedWaiters.delete(docId as string);
      for (const waiter of waiters) {
        waiter.reject(new OverleafOtError(`otUpdateError for ${docId}: ${errorReason}`, { cause: payload }));
      }
    }
  }

  /** Rejects EVERY pending applyUpdate() waiter across every doc -- used on
   * a transport-level "error" or "disconnect" (see _attachEventRouting),
   * where there's no single doc the failure is scoped to, unlike
   * otUpdateError which names one. Each waiter's own `reject` wrapper
   * already clears its own timer (see _waitForApplied). */
  _rejectAllAppliedWaiters(err: Error): void {
    for (const waiters of this._appliedWaiters.values()) {
      for (const waiter of waiters) waiter.reject(err);
    }
    this._appliedWaiters.clear();
  }

  close(): void {
    this.transport.close();
  }
}

/** A RangesTracker id seed is documented (outline.js/store.js's own prior
 * art in this same repo uses a similar convention elsewhere) as an 18-hex-
 * char value -- the first 18 characters of a Mongo ObjectId-shaped id,
 * matching `RangesTracker.generateIdSeed()`'s own confirmed format (read
 * directly from @overleaf/ranges-tracker's real source, not guessed):
 * 8 hex chars of timestamp + 6 of a random "machine" component + 4 of a
 * random "pid" component. This client only needs a value that's a
 * plausible-shaped, sufficiently-unique seed for the server to accept as
 * `meta.tc` -- RangesManager.applyUpdate only checks `Boolean(update.meta?.tc)`
 * for the on/off gate and passes the raw string straight to
 * `rangesTracker.setIdSeed()`, so exact byte-for-byte format parity with
 * Overleaf's own generator is not required for correctness, only a
 * same-shaped (18 hex chars), sufficiently-random string. */
function generateTcIdSeed(): string {
  const timestamp = Math.floor(Date.now() / 1000)
    .toString(16)
    .padStart(8, "0");
  const random = Math.floor(Math.random() * 0xffffffffffff)
    .toString(16)
    .padStart(10, "0")
    .slice(0, 10);
  return `${timestamp}${random}`;
}
