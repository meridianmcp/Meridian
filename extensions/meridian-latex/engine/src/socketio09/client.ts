// Original implementation of a minimal Socket.IO "0.9.x" CLIENT on top of
// the modern, maintained `ws` package -- see packet.js's own header comment
// for why this era of the protocol needs hand-written framing (no official
// spec, no safe-to-depend-on npm package for it) and why this is built on
// `ws` rather than the historic `socket.io-client@0.9.x` package (pulls in
// ws@0.4.x plus several now-vulnerable, browser-oriented transitive deps).
//
// Scope: WebSocket transport only (no XHR-polling fallback -- Overleaf's
// own client prefers websocket and this is a server-side/agent client with
// no need to work around corporate proxies the way a browser client does).
// `reconnect: false` throughout, matching Overleaf's own connection-
// manager.ts: a dropped connection surfaces as a clean failure/close event
// for the CALLER to decide whether and how to reconnect, never a silent
// automatic retry loop hiding real connection loss.

import { EventEmitter } from "node:events";
import { WebSocket as DefaultWebSocketImpl } from "ws";
import { encodePacket, decodePayload, type DecodedPacket } from "./packet.js";

const DEFAULT_RESOURCE = "socket.io";
const DEFAULT_HANDSHAKE_TIMEOUT_MS = 10_000;
const DEFAULT_ACK_TIMEOUT_MS = 15_000;

/** The minimal shape this module needs from a `fetch`-like function's
 * resolved response: real global `fetch`'s `Response` satisfies this, and so
 * does the tiny hand-built fake object this file's own test suite injects
 * (`{ok, status, text}`, optionally `headers.getSetCookie`) -- same
 * duck-typed real-or-fake precedent as zotero.ts's own `FetchResponseLike`. */
export interface HandshakeFetchResponseLike {
  ok: boolean;
  status: number;
  text(): Promise<string>;
  headers?: { getSetCookie?: () => string[] };
}

export type HandshakeFetchLike = (
  url: string,
  init: { headers: Record<string, string>; signal: AbortSignal },
) => Promise<HandshakeFetchResponseLike>;

export interface HandshakeOptions {
  /** e.g. "https://www.overleaf.com" */
  httpBaseUrl: string;
  resource?: string;
  query?: string;
  headers?: Record<string, string>;
  fetchImpl?: HandshakeFetchLike;
  timeoutMs?: number;
}

export interface ParsedHandshakeResponse {
  sessionId: string;
  heartbeatTimeoutMs: number | null;
  closeTimeoutMs: number | null;
  transports: string[];
}

export interface HandshakeResult extends ParsedHandshakeResponse {
  sessionCookies: string[];
}

/**
 * Performs the Socket.IO 0.9.x HTTP handshake: a plain GET to
 * `<httpBaseUrl>/<resource>/1/?<query>`, whose plain-text response body is
 * `<sessionId>:<heartbeatTimeoutSeconds>:<closeTimeoutSeconds>:<transport1,transport2,...>`.
 * Auth here is whatever `headers` carries (a `Cookie` header for a real
 * Overleaf session) -- this function has no opinion on where that cookie
 * comes from.
 *
 * Real bug found live 2026-09-25, first real connection attempt against
 * Overleaf's actual production infra: `www.overleaf.com` sits behind a
 * Google Cloud Load Balancer (`via: 1.1 google` on every response) that has
 * NO shared session store across backend instances for this handshake's
 * sessionId -- it uses a `GCLB=...` sticky-session cookie, set on THIS
 * handshake response, to route a later request back to the SAME backend.
 * Confirmed by direct reproduction: a WS upgrade sent with only the
 * Overleaf auth cookie (no GCLB cookie) got a real 101 Switching Protocols
 * (the raw WebSocket layer doesn't care), then immediately received
 * Socket.IO 0.9.x's own `7:::1+0` error packet (reason index 1 =
 * "client not handshaken", advice index 0 = "reconnect" -- these are
 * Overleaf's own Socket.IO fork's fixed `errorReasons`/`errorAdvices`
 * arrays) and closed with code 1006, before ANY other packet arrived --
 * i.e. the WS upgrade physically succeeded but landed on a DIFFERENT
 * backend instance than the one that minted the sessionId, which
 * legitimately doesn't recognize it. Forwarding the handshake response's
 * own `Set-Cookie` value(s) on the follow-up WS upgrade request (see
 * `connect()` below) fixed this on every subsequent attempt. This is
 * infra-specific to how Overleaf is deployed (a GCLB in front of a
 * stateful real-time tier), not a protocol-spec detail -- nothing in the
 * confirmed Socket.IO 0.9.x wire protocol itself requires this, so it's
 * documented here rather than in the protocol-research decision trail.
 */
export async function handshake({
  httpBaseUrl,
  resource = DEFAULT_RESOURCE,
  query = "",
  headers = {},
  fetchImpl = fetch,
  timeoutMs = DEFAULT_HANDSHAKE_TIMEOUT_MS,
}: HandshakeOptions): Promise<HandshakeResult> {
  const url = `${httpBaseUrl}/${resource}/1/${query ? `?${query}` : ""}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response: HandshakeFetchResponseLike;
  try {
    response = await fetchImpl(url, { headers, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    throw new Error(`socketio09 handshake failed: HTTP ${response.status} from ${url}`);
  }
  const body = await response.text();
  // `getSetCookie()` (Node 18.14+/undici) is the only correct way to read
  // MULTIPLE Set-Cookie headers -- they can't be safely comma-joined the
  // way other repeated headers can, since a cookie's own Expires attribute
  // legally contains a comma. Older/non-fetch-standard `headers` objects
  // (never true for the real global fetch this project requires Node 20+
  // for) simply yield no session cookies, same as a response with none.
  const setCookies = typeof response.headers?.getSetCookie === "function" ? response.headers.getSetCookie() : [];
  const sessionCookies = setCookies.map((c) => c.split(";")[0].trim()).filter(Boolean);
  return { ...parseHandshakeResponse(body), sessionCookies };
}

/** Parses a handshake response body into its four colon-separated fields.
 * Exported separately from `handshake()` so the parsing logic (the part
 * with real edge cases -- empty timeout fields, an empty transport list)
 * is unit-testable without a network call. */
export function parseHandshakeResponse(body: unknown): ParsedHandshakeResponse {
  const [sessionId, heartbeatTimeoutSeconds, closeTimeoutSeconds, transportsField] = String(body)
    .trim()
    .split(":");
  if (!sessionId) {
    throw new Error(`socketio09: malformed handshake response: ${JSON.stringify(body)}`);
  }
  return {
    sessionId,
    heartbeatTimeoutMs: secondsFieldToMs(heartbeatTimeoutSeconds),
    closeTimeoutMs: secondsFieldToMs(closeTimeoutSeconds),
    transports: transportsField ? transportsField.split(",").filter(Boolean) : [],
  };
}

function secondsFieldToMs(field: string | undefined): number | null {
  if (!field) return null;
  const seconds = Number(field);
  return Number.isFinite(seconds) ? seconds * 1000 : null;
}

/** The minimal shape this module needs from a WebSocket connection --
 * real `ws`'s `WebSocket` satisfies this (via its own generic,
 * `EventEmitter`-inherited `on()` overload as well as its per-event ones),
 * and so does this file's own test suite's hand-built `FakeWebSocket`
 * (also a plain `node:events` `EventEmitter` underneath). `message`'s data
 * is typed `string | Buffer`, not the real `ws` `RawData` union exactly --
 * this client only ever handles a text frame (a real Buffer, decoded via
 * `.toString("utf8")`) or a raw string (what the test double emits
 * directly) -- see the "message" handler below. */
export interface WebSocketLike {
  readonly readyState: number;
  send(data: string): void;
  close(): void;
  on(event: "open", listener: () => void): this;
  on(event: "message", listener: (data: string | Buffer) => void): this;
  on(event: "close", listener: (code: number, reason: Buffer) => void): this;
  on(event: "error", listener: (err: Error) => void): this;
}

/** The minimal shape of a WebSocket *constructor* this module needs --
 * real `ws`'s exported `WebSocket` class satisfies this (its own `OPEN`
 * static plus its `(url, options)` constructor overload), and so does this
 * file's own test suite's `FakeWebSocket` class. */
export interface WebSocketImplLike {
  readonly OPEN: number;
  new (url: string, options?: { headers?: Record<string, string> }): WebSocketLike;
}

export interface Socket09ClientOptions {
  /** e.g. "https://www.overleaf.com" */
  httpBaseUrl: string;
  /** e.g. "wss://www.overleaf.com" (kept separate from httpBaseUrl since a
   * caller might reasonably run these through different proxies/ports in a
   * self-hosted setup) */
  wsBaseUrl: string;
  /** default "socket.io" */
  resource?: string;
  /** raw query string, e.g. "projectId=<id>" -- sent on BOTH the handshake
   * and the websocket upgrade, matching Overleaf's own client behavior */
  query?: string;
  /** sent on the handshake request; the `Cookie` header here is also
   * forwarded to the WebSocket upgrade request automatically by `ws` */
  headers?: Record<string, string>;
  /** injectable for testing; defaults to the real `ws` WebSocket */
  WebSocketImpl?: WebSocketImplLike;
  /** injectable for testing; defaults to the global fetch */
  fetchImpl?: HandshakeFetchLike;
  ackTimeoutMs?: number;
}

interface PendingAck {
  resolve: (args: unknown[]) => void;
  reject: (err: Error) => void;
  timer: NodeJS.Timeout;
}

/**
 * A single Socket.IO 0.9.x connection over a WebSocket, default namespace
 * only (Overleaf's own protocol never uses custom endpoints/namespaces per
 * the research trail -- every packet's `endpoint` field is empty).
 *
 * Emits (via EventEmitter):
 *   - "connect"     -- the server's initial `1::` connect packet arrived; the socket is ready for emit()/emitWithAck()
 *   - "event"       ({name, args}) -- ANY server-pushed named event (joinProjectResponse, otUpdateApplied, otUpdateError, toggle-track-changes, ...)
 *   - "disconnect"  ({code, reason}) -- the underlying WebSocket closed, for any reason
 *   - "error"       (Error) -- a transport-level failure (handshake failure, WebSocket error event, ack timeout)
 *
 * Never auto-reconnects -- see module header. A caller that wants
 * reconnection drives it explicitly (new handshake, new Socket09Client).
 */
export class Socket09Client extends EventEmitter {
  httpBaseUrl: string;
  wsBaseUrl: string;
  resource: string;
  query: string;
  headers: Record<string, string>;
  WebSocketImpl: WebSocketImplLike;
  fetchImpl: HandshakeFetchLike;
  ackTimeoutMs: number;

  sessionId: string | null;
  ws: WebSocketLike | null;
  connected: boolean;
  private _nextAckId: number;
  private _pendingAcks: Map<string, PendingAck>;

  constructor({
    httpBaseUrl,
    wsBaseUrl,
    resource = DEFAULT_RESOURCE,
    query = "",
    headers = {},
    WebSocketImpl = DefaultWebSocketImpl,
    fetchImpl = fetch,
    ackTimeoutMs = DEFAULT_ACK_TIMEOUT_MS,
  }: Socket09ClientOptions) {
    super();
    this.httpBaseUrl = httpBaseUrl;
    this.wsBaseUrl = wsBaseUrl;
    this.resource = resource;
    this.query = query;
    this.headers = headers;
    this.WebSocketImpl = WebSocketImpl;
    this.fetchImpl = fetchImpl;
    this.ackTimeoutMs = ackTimeoutMs;

    this.sessionId = null;
    this.ws = null;
    this.connected = false;
    this._nextAckId = 1;
    this._pendingAcks = new Map(); // ackId (string) -> {resolve, reject, timer}
  }

  /** Performs the handshake, then opens the WebSocket and resolves once the
   * server's `1::` connect packet arrives (i.e. the socket is genuinely
   * ready, not merely TCP/TLS-connected). Rejects on any failure at either
   * step -- a caller should treat rejection as "never connected", not as
   * "connected then immediately disconnected". */
  async connect(): Promise<void> {
    const hs = await handshake({
      httpBaseUrl: this.httpBaseUrl,
      resource: this.resource,
      query: this.query,
      headers: this.headers,
      fetchImpl: this.fetchImpl,
    });
    this.sessionId = hs.sessionId;

    const wsUrl = `${this.wsBaseUrl}/${this.resource}/1/websocket/${hs.sessionId}${
      this.query ? `?${this.query}` : ""
    }`;

    // Forward the handshake's own session-affinity cookie(s) (see
    // handshake()'s own doc comment -- the real, live-found GCLB bug) so
    // the WS upgrade lands on the SAME backend that minted this sessionId.
    // Appended after this.headers' own Cookie value, not replacing it --
    // both the real Overleaf auth cookie AND the affinity cookie are
    // required on the upgrade request.
    const wsHeaders: Record<string, string> =
      hs.sessionCookies.length > 0
        ? {
            ...this.headers,
            Cookie: [this.headers.Cookie, ...hs.sessionCookies].filter(Boolean).join("; "),
          }
        : this.headers;

    return new Promise((resolve, reject) => {
      const ws = new this.WebSocketImpl(wsUrl, { headers: wsHeaders });
      this.ws = ws;

      let settled = false;
      const settleReject = (err: Error): void => {
        if (settled) return;
        settled = true;
        reject(err);
      };

      ws.on("open", () => {
        // Not yet "connected" in the Socket.IO sense -- wait for the
        // server's own connect packet below, matching the real protocol's
        // own notion of readiness (a raw WebSocket open is just transport,
        // not an application-level handshake ack).
      });

      ws.on("message", (raw) => {
        const text = typeof raw === "string" ? raw : raw.toString("utf8");
        for (const packet of decodePayload(text)) {
          // Real bug found 2026-09-18 via independent code review: this
          // used to call _handlePacket() (which emits "connect" for a
          // connect packet) BEFORE setting this.connected = true, so a
          // listener checking `client.connected` synchronously inside its
          // own "connect" handler would see stale `false`. Set the flag
          // first so it's already correct by the time any listener runs.
          if (!settled && packet.type === "connect") {
            settled = true;
            this.connected = true;
          }
          this._handlePacket(packet);
          if (packet.type === "connect") resolve();
        }
      });

      ws.on("close", (code, reasonBuf) => {
        this.connected = false;
        this._rejectAllPendingAcks(new Error("socketio09: connection closed"));
        this.emit("disconnect", { code, reason: reasonBuf ? reasonBuf.toString("utf8") : "" });
        settleReject(new Error(`socketio09: connection closed before connect (code ${code})`));
      });

      ws.on("error", (err) => {
        this.emit("error", err);
        settleReject(err);
      });
    });
  }

  _handlePacket(packet: DecodedPacket): void {
    switch (packet.type) {
      case "heartbeat":
        // Real bug found 2026-09-18 via independent code review: _send()
        // throws if the socket isn't OPEN, and this call sat unguarded
        // directly inside the raw WebSocket's own synchronous "message"
        // handler chain, with no try/catch anywhere between here and `ws`
        // itself. A heartbeat arriving in the brief window while the
        // connection is already closing (a real, if narrow, timing case --
        // not a client bug to react to) could throw an uncaught exception
        // out of that chain. A failed heartbeat reply on a dying connection
        // isn't actionable -- the close/disconnect handling already covers
        // the connection genuinely going away -- so this is swallowed, not
        // rethrown or emitted as "error".
        try {
          this._send(encodePacket({ type: "heartbeat" }));
        } catch {
          // socket already closing/closed -- nothing to do, see above.
        }
        break;
      case "connect":
        this.emit("connect");
        break;
      case "event":
        this.emit("event", { name: packet.name, args: packet.args || [] });
        break;
      case "ack":
        this._resolveAck(packet.ackId, packet.args || []);
        break;
      case "error":
        this.emit("error", new Error(`socketio09 server error: reason=${packet.reason} advice=${packet.advice}`));
        break;
      case "disconnect":
        // Server-initiated graceful disconnect of the default namespace --
        // the underlying WebSocket's own "close" event (handled above)
        // covers actually tearing the connection down; nothing further to
        // do here beyond letting a listener observe it if it cares to.
        this.emit("event", { name: "__socketio_disconnect__", args: [] });
        break;
      default:
        // Unrecognized/unparseable packet -- see packet.js's own
        // never-throw contract. Surface it for visibility without ever
        // crashing the connection over one bad frame.
        this.emit("error", new Error(`socketio09: unrecognized packet: ${JSON.stringify(packet)}`));
    }
  }

  _send(wire: string): void {
    if (!this.ws || this.ws.readyState !== this.WebSocketImpl.OPEN) {
      throw new Error("socketio09: cannot send, socket is not open");
    }
    this.ws.send(wire);
  }

  /** Fire-and-forget event emission -- no ack requested, no response
   * expected. Used for events Overleaf's own protocol never acks (there
   * are none of these in the confirmed protocol trail so far; kept for
   * completeness/symmetry with emitWithAck). */
  emitEvent(name: string, args: unknown[] = []): void {
    this._send(encodePacket({ type: "event", name, args }));
  }

  /**
   * Emits an event WITH an ack request (the shape every real Overleaf
   * client call needs -- joinDoc/applyOtUpdate both take a callback
   * server-side). Resolves with the ack's `args` array (matching a Node
   * callback's own `(err, ...results)` convention loosely -- callers
   * should treat `args[0]` as a possible error per Overleaf's own
   * callback-based API, since that's how joinDoc's real callback is
   * documented: `(err, lines, version, ...)`.
   *
   * Rejects on timeout (the server never acked) or if the socket closes
   * before the ack arrives -- never hangs forever.
   */
  emitWithAck(name: string, args: unknown[] = []): Promise<unknown[]> {
    const ackId = String(this._nextAckId++);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pendingAcks.delete(ackId);
        reject(new Error(`socketio09: ack timeout waiting for "${name}" (id ${ackId})`));
      }, this.ackTimeoutMs);
      this._pendingAcks.set(ackId, { resolve, reject, timer });

      try {
        this._send(encodePacket({ type: "event", id: ackId, ackRequested: true, name, args }));
      } catch (err) {
        clearTimeout(timer);
        this._pendingAcks.delete(ackId);
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    });
  }

  _resolveAck(ackId: string | null | undefined, args: unknown[]): void {
    if (ackId == null) return; // an ack with no id at all -- nothing to resolve.
    const pending = this._pendingAcks.get(ackId);
    if (!pending) return; // an ack for an id we're not (or no longer) waiting on -- ignore, don't throw
    clearTimeout(pending.timer);
    this._pendingAcks.delete(ackId);
    pending.resolve(args);
  }

  _rejectAllPendingAcks(err: Error): void {
    for (const { reject, timer } of this._pendingAcks.values()) {
      clearTimeout(timer);
      reject(err);
    }
    this._pendingAcks.clear();
  }

  close(): void {
    if (this.ws) this.ws.close();
  }
}
