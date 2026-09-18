import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { connectToProject, OverleafOtError, OverleafProjectSession } from "./overleaf-ot-client.js";

/** A fake Socket09Client: real EventEmitter for "event"/"disconnect", plus
 * a scriptable emitWithAck() so tests control exactly what each ack
 * resolves/rejects with, without any real network or WebSocket. */
class FakeTransport extends EventEmitter {
  constructor() {
    super();
    this.connected = false;
    this.emitWithAckCalls = [];
    this._ackQueue = [];
  }

  async connect() {
    this.connected = true;
  }

  close() {
    this.connected = false;
  }

  /** Test helper: queue the next N emitWithAck() calls' resolutions in
   * order (each entry is either {resolve: [...]} or {reject: Error}). */
  queueAck(entry) {
    this._ackQueue.push(entry);
  }

  emitWithAck(name, args) {
    this.emitWithAckCalls.push({ name, args });
    const next = this._ackQueue.shift();
    if (!next) return Promise.reject(new Error(`FakeTransport: no queued ack for "${name}"`));
    if (next.reject) return Promise.reject(next.reject);
    return Promise.resolve(next.resolve);
  }

  /** Test helper: simulate a server-pushed event (joinProjectResponse,
   * otUpdateApplied, otUpdateError, toggle-track-changes). */
  pushEvent(name, args) {
    this.emit("event", { name, args });
  }
}

function makeSession(transport = new FakeTransport()) {
  return new OverleafProjectSession({ projectId: "proj1", transport, appliedTimeoutMs: 100 });
}

// --- connectToProject(): the join-project handshake -----------------------

test("connectToProject(): resolves once joinProjectResponse arrives, capturing publicId + trackChangesState", async () => {
  class InstrumentedTransport extends FakeTransport {
    async connect() {
      this.connected = true;
      // Simulate the server's real behavior: joinProjectResponse arrives
      // unprompted right after connect, asynchronously.
      setTimeout(() => this.pushEvent("joinProjectResponse", [
        { publicId: "pub1", project: { trackChangesState: { user1: true, __guests__: false } } },
      ]), 5);
    }
  }
  const session = await connectToProject({
    projectId: "proj1",
    httpBaseUrl: "https://www.overleaf.com",
    wsBaseUrl: "wss://www.overleaf.com",
    cookie: "sharelatex.sid=abc",
    Socket09ClientImpl: InstrumentedTransport,
    appliedTimeoutMs: 500,
  });
  assert.equal(session.publicId, "pub1");
  assert.equal(session.trackChangesOnForUser("user1"), true);
  assert.equal(session.trackChangesOnForUser("someoneElse"), false); // falls back to __guests__
});

test("connectToProject(): requires a projectId and a cookie", async () => {
  await assert.rejects(
    () => connectToProject({ httpBaseUrl: "x", wsBaseUrl: "x", cookie: "c" }),
    OverleafOtError,
  );
  await assert.rejects(
    () => connectToProject({ projectId: "p", httpBaseUrl: "x", wsBaseUrl: "x" }),
    OverleafOtError,
  );
});

test("connectToProject(): times out if joinProjectResponse never arrives", async () => {
  class SilentTransport extends FakeTransport {}
  await assert.rejects(
    () =>
      connectToProject({
        projectId: "proj1",
        httpBaseUrl: "x",
        wsBaseUrl: "x",
        cookie: "c",
        Socket09ClientImpl: SilentTransport,
        appliedTimeoutMs: 30,
      }),
    /timed out/,
  );
});

// --- joinDoc() -------------------------------------------------------------

test("joinDoc(): always requests fromVersion 0 and returns the full lines + version + ranges", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: [null, ["line one", "line two"], 7, [], { changes: [] }, "sharejs-text-ot"] });
  const session = makeSession(transport);

  const result = await session.joinDoc("doc1");
  assert.deepEqual(transport.emitWithAckCalls[0].args, ["doc1", 0, {}]);
  assert.deepEqual(result.lines, ["line one", "line two"]);
  assert.equal(result.version, 7);
  assert.deepEqual(result.ranges, { changes: [] });
  assert.equal(result.docType, "sharejs-text-ot");
});

test("joinDoc(): a server-side error in the ack throws OverleafOtError", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: ["not found"] });
  const session = makeSession(transport);
  await assert.rejects(() => session.joinDoc("doc1"), OverleafOtError);
});

// --- applyUpdate(): the safety-critical write path -------------------------

test("applyUpdate(): resolves only after BOTH the ack AND the matching otUpdateApplied broadcast arrive", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: [null] }); // bare ack, no error
  const session = makeSession(transport);

  const updatePromise = session.applyUpdate("doc1", 5, [{ i: "hello", p: 0 }]);

  // Not yet resolved -- only the ack has landed, not the applied broadcast.
  let settled = false;
  updatePromise.then(() => { settled = true; });
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(settled, false, "must not resolve on the ack alone");

  transport.pushEvent("otUpdateApplied", [{ v: 6, doc: "doc1" }]);
  const result = await updatePromise;
  assert.equal(result.version, 6);
});

test("applyUpdate(): sets meta.tc when trackChanges is requested, omits it otherwise", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: [null] });
  const session = makeSession(transport);

  const promise = session.applyUpdate("doc1", 5, [{ i: "x", p: 0 }], true);
  const sentUpdate = transport.emitWithAckCalls[0].args[1];
  assert.equal(typeof sentUpdate.meta.tc, "string");
  assert.match(sentUpdate.meta.tc, /^[0-9a-f]{18}$/);

  transport.pushEvent("otUpdateApplied", [{ v: 6, doc: "doc1" }]);
  await promise;
});

test("applyUpdate(): trackChanges=false (default) sends no meta field at all", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: [null] });
  const session = makeSession(transport);
  const promise = session.applyUpdate("doc1", 5, [{ i: "x", p: 0 }]);
  const sentUpdate = transport.emitWithAckCalls[0].args[1];
  assert.equal(sentUpdate.meta, undefined);
  transport.pushEvent("otUpdateApplied", [{ v: 6, doc: "doc1" }]);
  await promise;
});

test("applyUpdate(): an ack-level error rejects immediately, never waits for a broadcast", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: ["Op too old"] });
  const session = makeSession(transport);
  await assert.rejects(() => session.applyUpdate("doc1", 5, [{ i: "x", p: 0 }]), OverleafOtError);
});

test("applyUpdate(): an otUpdateError broadcast for this doc rejects the pending write", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: [null] });
  const session = makeSession(transport);

  const promise = session.applyUpdate("doc1", 5, [{ i: "x", p: 0 }]);
  transport.pushEvent("otUpdateError", ["Op at future version", { project_id: "proj1", doc_id: "doc1" }]);
  await assert.rejects(() => promise, OverleafOtError);
});

test("applyUpdate(): an otUpdateApplied for a DIFFERENT doc is ignored, not mistaken for this write's confirmation", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: [null] });
  const session = makeSession(transport);

  const promise = session.applyUpdate("doc1", 5, [{ i: "x", p: 0 }]);
  transport.pushEvent("otUpdateApplied", [{ v: 6, doc: "some-other-doc" }]);

  let settled = false;
  promise.then(() => { settled = true; });
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(settled, false);

  transport.pushEvent("otUpdateApplied", [{ v: 6, doc: "doc1" }]);
  await promise;
});

test("applyUpdate(): an otUpdateApplied with the WRONG version for this doc is ignored (another client's concurrent write), keeps waiting for ours", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: [null] });
  const session = makeSession(transport);

  const promise = session.applyUpdate("doc1", 5, [{ i: "x", p: 0 }]); // expects resulting version 6
  transport.pushEvent("otUpdateApplied", [{ v: 9, doc: "doc1" }]); // someone else's concurrent write landed first

  let settled = false;
  promise.then(() => { settled = true; });
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(settled, false, "a mismatched version must not be mistaken for this write's confirmation");

  transport.pushEvent("otUpdateApplied", [{ v: 6, doc: "doc1" }]);
  await promise;
});

test("applyUpdate(): times out if no otUpdateApplied ever arrives for this write", async () => {
  const transport = new FakeTransport();
  transport.queueAck({ resolve: [null] });
  const session = makeSession(transport);
  await assert.rejects(() => session.applyUpdate("doc1", 5, [{ i: "x", p: 0 }]), /timed out/);
});

test("toggle-track-changes event live-updates trackChangesState", () => {
  const transport = new FakeTransport();
  const session = makeSession(transport); // event routing is wired in the constructor itself
  assert.equal(session.trackChangesOnForUser("user1"), false);
  transport.pushEvent("toggle-track-changes", [{ user1: true }]);
  assert.equal(session.trackChangesOnForUser("user1"), true);
});
