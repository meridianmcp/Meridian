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
import { encodePacket, decodePayload } from "./packet.js";

const DEFAULT_RESOURCE = "socket.io";
const DEFAULT_HANDSHAKE_TIMEOUT_MS = 10_000;
const DEFAULT_ACK_TIMEOUT_MS = 15_000;

/**
 * Performs the Socket.IO 0.9.x HTTP handshake: a plain GET to
 * `<httpBaseUrl>/<resource>/1/?<query>`, whose plain-text response body is
 * `<sessionId>:<heartbeatTimeoutSeconds>:<closeTimeoutSeconds>:<transport1,transport2,...>`.
 * Auth here is whatever `headers` carries (a `Cookie` header for a real
 * Overleaf session) -- this function has no opinion on where that cookie
 * comes from.
 *
 * @returns {Promise<{sessionId: string, heartbeatTimeoutMs: number|null, closeTimeoutMs: number|null, transports: string[]}>}
 */
export async function handshake({
  httpBaseUrl,
  resource = DEFAULT_RESOURCE,
  query = "",
  headers = {},
  fetchImpl = fetch,
  timeoutMs = DEFAULT_HANDSHAKE_TIMEOUT_MS,
}) {
  const url = `${httpBaseUrl}/${resource}/1/${query ? `?${query}` : ""}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetchImpl(url, { headers, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    throw new Error(`socketio09 handshake failed: HTTP ${response.status} from ${url}`);
  }
  const body = await response.text();
  return parseHandshakeResponse(body);
}

/** Parses a handshake response body into its four colon-separated fields.
 * Exported separately from `handshake()` so the parsing logic (the part
 * with real edge cases -- empty timeout fields, an empty transport list)
 * is unit-testable without a network call. */
export function parseHandshakeResponse(body) {
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

function secondsFieldToMs(field) {
  if (!field) return null;
  const seconds = Number(field);
  return Number.isFinite(seconds) ? seconds * 1000 : null;
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
  /**
   * @param {object} opts
   * @param {string} opts.httpBaseUrl   e.g. "https://www.overleaf.com"
   * @param {string} opts.wsBaseUrl     e.g. "wss://www.overleaf.com" (kept separate from httpBaseUrl since a caller might reasonably run these through different proxies/ports in a self-hosted setup)
   * @param {string} [opts.resource]    default "socket.io"
   * @param {string} [opts.query]       raw query string, e.g. "projectId=<id>" -- sent on BOTH the handshake and the websocket upgrade, matching Overleaf's own client behavior
   * @param {Record<string,string>} [opts.headers]  sent on the handshake request; the `Cookie` header here is also forwarded to the WebSocket upgrade request automatically by `ws`
   * @param {typeof import("ws").WebSocket} [opts.WebSocketImpl]  injectable for testing; defaults to the real `ws` WebSocket
   * @param {typeof fetch} [opts.fetchImpl]  injectable for testing; defaults to the global fetch
   * @param {number} [opts.ackTimeoutMs]
   */
  constructor({
    httpBaseUrl,
    wsBaseUrl,
    resource = DEFAULT_RESOURCE,
    query = "",
    headers = {},
    WebSocketImpl = DefaultWebSocketImpl,
    fetchImpl = fetch,
    ackTimeoutMs = DEFAULT_ACK_TIMEOUT_MS,
  }) {
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
  async connect() {
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

    return new Promise((resolve, reject) => {
      const ws = new this.WebSocketImpl(wsUrl, { headers: this.headers });
      this.ws = ws;

      let settled = false;
      const settleReject = (err) => {
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

  _handlePacket(packet) {
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

  _send(wire) {
    if (!this.ws || this.ws.readyState !== this.WebSocketImpl.OPEN) {
      throw new Error("socketio09: cannot send, socket is not open");
    }
    this.ws.send(wire);
  }

  /** Fire-and-forget event emission -- no ack requested, no response
   * expected. Used for events Overleaf's own protocol never acks (there
   * are none of these in the confirmed protocol trail so far; kept for
   * completeness/symmetry with emitWithAck). */
  emitEvent(name, args = []) {
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
  emitWithAck(name, args = []) {
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
        reject(err);
      }
    });
  }

  _resolveAck(ackId, args) {
    const pending = this._pendingAcks.get(ackId);
    if (!pending) return; // an ack for an id we're not (or no longer) waiting on -- ignore, don't throw
    clearTimeout(pending.timer);
    this._pendingAcks.delete(ackId);
    pending.resolve(args);
  }

  _rejectAllPendingAcks(err) {
    for (const { reject, timer } of this._pendingAcks.values()) {
      clearTimeout(timer);
      reject(err);
    }
    this._pendingAcks.clear();
  }

  close() {
    if (this.ws) this.ws.close();
  }
}
