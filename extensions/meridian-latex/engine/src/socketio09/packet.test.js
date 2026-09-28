import { test } from "node:test";
import assert from "node:assert/strict";
import { encodePacket, decodePacket, decodePayload, PACKET_TYPES } from "./packet.js";

test("packet type numbers match the fixed protocol grammar", () => {
  assert.equal(PACKET_TYPES.disconnect, 0);
  assert.equal(PACKET_TYPES.connect, 1);
  assert.equal(PACKET_TYPES.heartbeat, 2);
  assert.equal(PACKET_TYPES.message, 3);
  assert.equal(PACKET_TYPES.json, 4);
  assert.equal(PACKET_TYPES.event, 5);
  assert.equal(PACKET_TYPES.ack, 6);
  assert.equal(PACKET_TYPES.error, 7);
  assert.equal(PACKET_TYPES.noop, 8);
});

test("encodePacket: heartbeat is the bare wire form", () => {
  assert.equal(encodePacket({ type: "heartbeat" }), "2::");
});

test("encodePacket: connect with a query string", () => {
  // type:id:endpoint:data -- id and endpoint are both empty here, but each
  // still occupies its own colon-delimited segment before the data.
  assert.equal(
    encodePacket({ type: "connect", query: "projectId=abc123" }),
    "1:::projectId=abc123",
  );
});

test("encodePacket: connect with no query is the bare form", () => {
  assert.equal(encodePacket({ type: "connect" }), "1::");
});

test("encodePacket: event without ack, no args", () => {
  assert.equal(encodePacket({ type: "event", name: "ping" }), '5:::{"name":"ping"}');
});

test("encodePacket: event WITH ack requested carries the id and a trailing +", () => {
  const wire = encodePacket({
    type: "event",
    id: 7,
    ackRequested: true,
    name: "applyOtUpdate",
    args: ["doc123", { v: 5, op: [{ i: "hi", p: 0 }] }],
  });
  assert.equal(wire, '5:7+::{"name":"applyOtUpdate","args":["doc123",{"v":5,"op":[{"i":"hi","p":0}]}]}');
  // Independently confirm via decode, so a matching mistake in both the
  // encoder AND this hand-typed expectation can't cancel out silently.
  const decoded = decodePacket(wire);
  assert.equal(decoded.id, "7");
  assert.equal(decoded.ackRequested, true);
  assert.equal(decoded.name, "applyOtUpdate");
  assert.deepEqual(decoded.args, ["doc123", { v: 5, op: [{ i: "hi", p: 0 }] }]);
});

test("encodePacket: ack with args", () => {
  assert.equal(encodePacket({ type: "ack", ackId: "7", args: [{ v: 6 }] }), '6:::7+[{"v":6}]');
});

test("encodePacket: ack with no args omits the + and array entirely", () => {
  assert.equal(encodePacket({ type: "ack", ackId: "7" }), "6:::7");
});

test("encodePacket: message with data", () => {
  assert.equal(encodePacket({ type: "message", data: "hello" }), "3:::hello");
});

test("encodePacket: json data is JSON-stringified", () => {
  assert.equal(encodePacket({ type: "json", data: { a: 1 } }), '4:::{"a":1}');
});

test("encodePacket: unknown type throws rather than silently emitting garbage", () => {
  assert.throws(() => encodePacket({ type: "not-a-real-type" }));
});

test("decodePacket: heartbeat", () => {
  const packet = decodePacket("2::");
  assert.equal(packet.type, "heartbeat");
});

test("decodePacket: connect with query round-trips", () => {
  // Three colons: type:id:endpoint:data -- endpoint is empty (default
  // namespace) but still occupies its own segment before the data.
  const packet = decodePacket("1:::projectId=abc123");
  assert.equal(packet.type, "connect");
  assert.equal(packet.query, "projectId=abc123");
});

test("encodePacket output for a connect-with-query is decodable by decodePacket (round-trip, not just each direction eyeballed separately)", () => {
  const wire = encodePacket({ type: "connect", query: "projectId=abc123" });
  const decoded = decodePacket(wire);
  assert.equal(decoded.type, "connect");
  assert.equal(decoded.query, "projectId=abc123");
});

test("decodePacket: event round-trips name + args", () => {
  const packet = decodePacket('5:12+::{"name":"joinDoc","args":["doc123",5,{"age":0}]}');
  assert.equal(packet.type, "event");
  assert.equal(packet.id, "12");
  assert.equal(packet.ackRequested, true);
  assert.equal(packet.name, "joinDoc");
  assert.deepEqual(packet.args, ["doc123", 5, { age: 0 }]);
});

test("decodePacket: event with no args defaults args to an empty array", () => {
  const packet = decodePacket('5:::{"name":"ping"}');
  assert.equal(packet.type, "event");
  assert.deepEqual(packet.args, []);
});

test("decodePacket: ack with args round-trips", () => {
  const packet = decodePacket('6:::7+[{"v":6,"doc":"docId"}]');
  assert.equal(packet.type, "ack");
  assert.equal(packet.ackId, "7");
  assert.deepEqual(packet.args, [{ v: 6, doc: "docId" }]);
});

test("decodePacket: ack with no args", () => {
  const packet = decodePacket("6:::7");
  assert.equal(packet.type, "ack");
  assert.equal(packet.ackId, "7");
  assert.deepEqual(packet.args, []);
});

test("decodePacket: error packet with reason and advice", () => {
  const packet = decodePacket("7:::2+0");
  assert.equal(packet.type, "error");
  assert.equal(packet.reason, "2");
  assert.equal(packet.advice, "0");
});

test("decodePacket: unparseable garbage never throws, decodes to a null type", () => {
  assert.doesNotThrow(() => decodePacket("not a real packet at all"));
  const packet = decodePacket("not a real packet at all");
  assert.equal(packet.type, null);
});

test("decodePacket: malformed JSON in an event's data never throws", () => {
  const packet = decodePacket("5:::{not valid json");
  assert.equal(packet.type, "event");
  assert.deepEqual(packet.args, []);
});

test("decodePacket: empty string input never throws", () => {
  assert.doesNotThrow(() => decodePacket(""));
});

test("decodePacket: non-string input never throws", () => {
  assert.doesNotThrow(() => decodePacket(undefined));
  assert.doesNotThrow(() => decodePacket(null));
  assert.doesNotThrow(() => decodePacket(42));
});

test("encode -> decode round-trip for a real applyOtUpdate-shaped event", () => {
  const original = {
    type: "event",
    id: "3",
    ackRequested: true,
    name: "applyOtUpdate",
    args: [
      "doc123",
      { v: 17, op: [{ d: "old", p: 10 }, { i: "new", p: 10 }], meta: { tc: "000000000000000000" } },
    ],
  };
  const decoded = decodePacket(encodePacket(original));
  assert.equal(decoded.type, "event");
  assert.equal(decoded.id, "3");
  assert.equal(decoded.ackRequested, true);
  assert.equal(decoded.name, "applyOtUpdate");
  assert.deepEqual(decoded.args, original.args);
});

test("decodePayload: a single-packet payload (the common WebSocket-frame case)", () => {
  const packets = decodePayload("2::");
  assert.equal(packets.length, 1);
  assert.equal(packets[0].type, "heartbeat");
});

test("decodePayload: a multi-packet payload delimited by U+FFFD + length", () => {
  const p1 = "2::";
  const p2 = '5:::{"name":"ping"}';
  const payload = `�${p1.length}�${p1}�${p2.length}�${p2}`;
  const packets = decodePayload(payload);
  assert.equal(packets.length, 2);
  assert.equal(packets[0].type, "heartbeat");
  assert.equal(packets[1].type, "event");
  assert.equal(packets[1].name, "ping");
});

test("decodePayload: null/undefined input returns an empty array, never throws", () => {
  assert.deepEqual(decodePayload(null), []);
  assert.deepEqual(decodePayload(undefined), []);
});
