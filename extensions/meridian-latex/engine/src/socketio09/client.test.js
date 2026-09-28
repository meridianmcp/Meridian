import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { Socket09Client, parseHandshakeResponse, handshake } from "./client.js";

// --- parseHandshakeResponse: pure logic, no network -----------------------

test("parseHandshakeResponse: a real-shaped 4-field response", () => {
  const parsed = parseHandshakeResponse("abc123sessionid:60:60:websocket,xhr-polling");
  assert.equal(parsed.sessionId, "abc123sessionid");
  assert.equal(parsed.heartbeatTimeoutMs, 60_000);
  assert.equal(parsed.closeTimeoutMs, 60_000);
  assert.deepEqual(parsed.transports, ["websocket", "xhr-polling"]);
});

test("parseHandshakeResponse: trims surrounding whitespace/newlines", () => {
  const parsed = parseHandshakeResponse("  sessionid:60:60:websocket\n");
  assert.equal(parsed.sessionId, "sessionid");
});

test("parseHandshakeResponse: empty timeout fields become null, not NaN or 0", () => {
  const parsed = parseHandshakeResponse("sessionid:::websocket");
  assert.equal(parsed.heartbeatTimeoutMs, null);
  assert.equal(parsed.closeTimeoutMs, null);
});

test("parseHandshakeResponse: missing session id throws rather than returning a bogus object", () => {
  assert.throws(() => parseHandshakeResponse(":60:60:websocket"));
  assert.throws(() => parseHandshakeResponse(""));
});

test("handshake(): builds the correct URL and forwards headers", async () => {
  let capturedUrl, capturedOpts;
  const fakeFetch = async (url, opts) => {
    capturedUrl = url;
    capturedOpts = opts;
    return { ok: true, status: 200, text: async () => "sess1:60:60:websocket" };
  };
  const result = await handshake({
    httpBaseUrl: "https://www.overleaf.com",
    resource: "socket.io",
    query: "projectId=proj1",
    headers: { Cookie: "sharelatex.sid=abc" },
    fetchImpl: fakeFetch,
  });
  assert.equal(capturedUrl, "https://www.overleaf.com/socket.io/1/?projectId=proj1");
  assert.equal(capturedOpts.headers.Cookie, "sharelatex.sid=abc");
  assert.equal(result.sessionId, "sess1");
});

test("handshake(): a non-ok HTTP response throws with the status code", async () => {
  const fakeFetch = async () => ({ ok: false, status: 403, text: async () => "forbidden" });
  await assert.rejects(
    () => handshake({ httpBaseUrl: "https://www.overleaf.com", fetchImpl: fakeFetch }),
    /403/,
  );
});

// --- Socket09Client: fake WebSocket + fake fetch, no real network --------

class FakeWebSocket extends EventEmitter {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;

  constructor(url, opts) {
    super();
    this.url = url;
    this.opts = opts;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    FakeWebSocket.lastInstance = this;
  }

  send(data) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.emit("close", 1000, Buffer.from(""));
  }

  // --- test-only helpers to simulate server behavior ---
  simulateOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.emit("open");
  }

  simulateMessage(wireText) {
    this.emit("message", wireText);
  }
}

function fakeFetchFor(handshakeBody) {
  return async () => ({ ok: true, status: 200, text: async () => handshakeBody });
}

function makeClient(overrides = {}) {
  return new Socket09Client({
    httpBaseUrl: "https://www.overleaf.com",
    wsBaseUrl: "wss://www.overleaf.com",
    query: "projectId=proj1",
    headers: { Cookie: "sharelatex.sid=abc" },
    WebSocketImpl: FakeWebSocket,
    fetchImpl: fakeFetchFor("sess1:60:60:websocket"),
    ackTimeoutMs: 200,
    ...overrides,
  });
}

test("Socket09Client.connect(): resolves once the server's connect packet arrives, not merely on WS open", async () => {
  const client = makeClient();
  const connectPromise = client.connect();

  // Give connect() a tick to run the handshake and construct the fake WS.
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  assert.ok(ws, "a WebSocket instance should have been constructed");
  assert.equal(client.connected, false, "not connected before the server's own connect packet");

  ws.simulateOpen();
  assert.equal(client.connected, false, "raw WS open alone must not count as connected");

  ws.simulateMessage("1::");
  await connectPromise;
  assert.equal(client.connected, true);
});

test("Socket09Client.connect(): the WebSocket URL includes the handshake session id and query string", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  assert.equal(ws.url, "wss://www.overleaf.com/socket.io/1/websocket/sess1?projectId=proj1");
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;
});

test("Socket09Client: auto-responds to a heartbeat packet by echoing one back", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;

  ws.sent = []; // clear the pre-connect sent log for a clean assertion
  ws.simulateMessage("2::");
  assert.deepEqual(ws.sent, ["2::"]);
});

test("Socket09Client: server-pushed named events are re-emitted via the 'event' EventEmitter channel", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;

  const received = [];
  client.on("event", (e) => received.push(e));
  ws.simulateMessage('5:::{"name":"joinProjectResponse","args":[{"publicId":"p1"}]}');
  assert.equal(received.length, 1);
  assert.equal(received[0].name, "joinProjectResponse");
  assert.deepEqual(received[0].args, [{ publicId: "p1" }]);
});

test("Socket09Client.emitWithAck(): sends the correctly-shaped event packet and resolves with the ack's args", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;

  const ackPromise = client.emitWithAck("joinDoc", ["doc1", 0, {}]);
  assert.equal(ws.sent.length, 1);
  assert.match(ws.sent[0], /^5:1\+::/);
  const sentPacket = JSON.parse(ws.sent[0].split(/^5:1\+::/)[1]);
  assert.equal(sentPacket.name, "joinDoc");
  assert.deepEqual(sentPacket.args, ["doc1", 0, {}]);

  // Server acks with the SAME id (1), carrying (err=null, lines, version).
  ws.simulateMessage('6:::1+[null,["line1","line2"],5]');
  const result = await ackPromise;
  assert.deepEqual(result, [null, ["line1", "line2"], 5]);
});

test("Socket09Client.emitWithAck(): rejects on timeout when the server never acks", async () => {
  const client = makeClient({ ackTimeoutMs: 30 });
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;

  await assert.rejects(() => client.emitWithAck("applyOtUpdate", ["doc1", {}]), /timeout/);
});

test("Socket09Client: an ack for an unknown/stale id is silently ignored, never throws", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;

  assert.doesNotThrow(() => ws.simulateMessage("6:::999+[]"));
});

test("Socket09Client: closing the connection rejects every pending ack rather than hanging forever", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;

  const ackPromise = client.emitWithAck("applyOtUpdate", ["doc1", {}]);
  client.close();
  await assert.rejects(() => ackPromise, /closed/);
});

test("Socket09Client: a disconnect event fires with the close code/reason", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;

  const disconnects = [];
  client.on("disconnect", (d) => disconnects.push(d));
  client.close();
  assert.equal(disconnects.length, 1);
  assert.equal(disconnects[0].code, 1000);
});

// Real bugs found 2026-09-18 via independent code review.

test("Socket09Client: this.connected is already true by the time a 'connect' listener runs (not stale false)", async () => {
  const client = makeClient();
  let connectedInsideListener = null;
  client.on("connect", () => {
    connectedInsideListener = client.connected;
  });
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;
  assert.equal(connectedInsideListener, true, "a 'connect' listener must see the flag already set, not stale false");
});

test("Socket09Client: a heartbeat arriving while the socket is no longer OPEN does not throw (a real, narrow timing case, not a crash)", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.simulateMessage("1::");
  await connectPromise;

  // Simulate the narrow race: the socket has moved past OPEN (about to
  // close) but a heartbeat frame is still delivered on this same tick.
  ws.readyState = FakeWebSocket.CLOSED;
  assert.doesNotThrow(() => ws.simulateMessage("2::"));
});

test("Socket09Client.connect(): rejects if the socket closes before the server's connect packet ever arrives", async () => {
  const client = makeClient();
  const connectPromise = client.connect();
  await new Promise((r) => setTimeout(r, 10));
  const ws = FakeWebSocket.lastInstance;
  ws.simulateOpen();
  ws.close(); // closed before "1::" ever arrives
  await assert.rejects(() => connectPromise, /before connect/);
});
