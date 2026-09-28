// Original implementation of the legacy Socket.IO "0.9.x" wire framing --
// the transport Overleaf's real-time service actually runs (confirmed via
// its own package.json: "socket.io": "github:overleaf/socket.io#0.9.19-
// overleaf-12"), predating the modern socket.io-client/Engine.IO protocol
// entirely (no official spec exists for this era -- it predates the
// Engine.IO/Socket.IO split that the CURRENT socket.io-protocol spec
// documents starting from its own "v1"). This module implements the wire
// GRAMMAR (packet type numbers, field layout) as an interoperability
// detail -- the actual code below is original, written from protocol
// understanding, not copied from any implementation's source. See
// meridian-build project decision "Overleaf real-time OT protocol..." for
// the full research trail and why this approach (not netique/overleaf-mcp's
// own AGPL-licensed code, not the actual historic socket.io-client@0.9.x
// npm package -- which pulls ws@0.4.x plus several now-vulnerable
// transitive deps) was chosen.
//
// Packet type numbers are a fixed, closed set for this protocol era (not
// an arbitrary choice -- any two implementations of "Socket.IO 0.9.x" MUST
// agree on these exact numbers to interoperate at all, the same way any
// two implementations of HTTP must agree GET is the same verb):
//   0 disconnect, 1 connect, 2 heartbeat, 3 message, 4 json,
//   5 event, 6 ack, 7 error, 8 noop

export const PACKET_TYPES = Object.freeze({
  disconnect: 0,
  connect: 1,
  heartbeat: 2,
  message: 3,
  json: 4,
  event: 5,
  ack: 6,
  error: 7,
  noop: 8,
});

export const PACKET_TYPE_NAMES = Object.freeze(
  Object.fromEntries(Object.entries(PACKET_TYPES).map(([name, num]) => [num, name])),
);

/** Multi-packet payload framing uses U+FFFD (the Unicode replacement
 * character) as a length-prefixed delimiter between concatenated packets --
 * relevant when several packets are flushed as one payload (e.g. XHR-
 * polling batching several queued frames into one HTTP response body).
 * A single WebSocket text frame normally carries exactly one packet, so
 * this client never NEEDS to emit multi-packet payloads, but must still
 * DECODE one if the server ever sends it. */
const PAYLOAD_DELIMITER = "�";

/**
 * Encodes one packet object into its wire-format string.
 *
 * Grammar: `<type>:<id><+ if ack requires the raw id, i.e. "data" ack>:<endpoint>:<data>`
 * -- the data segment (and its leading colon) is omitted entirely when
 * there is nothing to send, not even an empty string after the colon.
 *
 * `packet` shape (by `type`):
 *   - "connect":    { endpoint?, query? }              -- query is a raw query string
 *   - "event":      { id?, ackRequested?, endpoint?, name, args? }
 *   - "ack":        { ackId, args? }
 *   - "heartbeat":  {}
 *   - "message":    { id?, endpoint?, data }
 *   - "json":       { id?, endpoint?, data }
 *   - "error":      { reason?, advice? }               -- pre-formatted numeric strings, rarely sent by a client
 *   - "disconnect": { endpoint? }
 *   - "noop":       {}
 */
export function encodePacket(packet) {
  const typeNumber = PACKET_TYPES[packet.type];
  if (typeNumber === undefined) {
    throw new Error(`socketio09.encodePacket: unknown packet type "${packet.type}"`);
  }

  const id = packet.id != null ? String(packet.id) : "";
  const ackMarker = packet.ackRequested ? "+" : "";
  const endpoint = packet.endpoint || "";
  let data;

  switch (packet.type) {
    case "connect":
      data = packet.query ? packet.query : undefined;
      break;
    case "message":
      data = packet.data !== "" ? packet.data : undefined;
      break;
    case "json":
      data = JSON.stringify(packet.data);
      break;
    case "event": {
      const payload = { name: packet.name };
      if (packet.args && packet.args.length) payload.args = packet.args;
      data = JSON.stringify(payload);
      break;
    }
    case "ack": {
      const ackArgs = packet.args && packet.args.length ? `+${JSON.stringify(packet.args)}` : "";
      data = `${packet.ackId}${ackArgs}`;
      break;
    }
    case "error": {
      const reason = packet.reason != null ? String(packet.reason) : "";
      const advice = packet.advice != null ? String(packet.advice) : "";
      data = reason || advice ? `${reason}${advice ? `+${advice}` : ""}` : undefined;
      break;
    }
    default:
      data = undefined;
  }

  let wire = `${typeNumber}:${id}${ackMarker}:${endpoint}`;
  if (data !== undefined && data !== null) wire += `:${data}`;
  return wire;
}

/**
 * Decodes one packet's wire-format string back into a structured object.
 * Mirrors `encodePacket`'s field shapes. Never throws -- an unparseable
 * frame decodes to `{type: null, raw}` so a caller can log/ignore it
 * rather than crash the whole connection over one malformed frame (a
 * heartbeat/keepalive channel should be resilient to this).
 */
export function decodePacket(raw) {
  if (typeof raw !== "string" || raw.length === 0) {
    return { type: null, raw };
  }

  // <type>:<id>?<+>?:<endpoint>?:<data>?
  const match = raw.match(/^([0-8]):([0-9]*)(\+)?:([^:]*):?([\s\S]*)$/);
  if (!match) return { type: null, raw };

  const [, typeDigits, idPart, ackPlus, endpoint, dataPart] = match;
  const type = PACKET_TYPE_NAMES[Number(typeDigits)];
  if (!type) return { type: null, raw };

  const packet = { type, endpoint: endpoint || "" };
  if (idPart) {
    packet.id = idPart;
    packet.ackRequested = Boolean(ackPlus);
  }

  switch (type) {
    case "message":
      packet.data = dataPart || "";
      break;
    case "json":
      packet.data = safeJsonParse(dataPart);
      break;
    case "connect":
      packet.query = dataPart || "";
      break;
    case "event": {
      const parsed = safeJsonParse(dataPart) || {};
      packet.name = parsed.name;
      packet.args = parsed.args || [];
      break;
    }
    case "ack": {
      const ackMatch = dataPart.match(/^([0-9]+)(\+)?([\s\S]*)$/);
      if (ackMatch) {
        packet.ackId = ackMatch[1];
        packet.args = ackMatch[3] ? safeJsonParse(ackMatch[3]) || [] : [];
      } else {
        packet.ackId = null;
        packet.args = [];
      }
      break;
    }
    case "error": {
      const [reason, advice] = (dataPart || "").split("+");
      packet.reason = reason || "";
      packet.advice = advice || "";
      break;
    }
    default:
      break;
  }

  return packet;
}

function safeJsonParse(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** Splits a raw WebSocket text-frame payload into one or more decoded
 * packets, transparently handling the U+FFFD-delimited multi-packet
 * payload framing (see PAYLOAD_DELIMITER above) if the server ever uses
 * it. A plain single-packet frame (the common case for a WebSocket
 * transport, as opposed to XHR-polling) decodes to a one-element array. */
export function decodePayload(raw) {
  if (raw == null) return [];
  if (raw[0] !== PAYLOAD_DELIMITER) return [decodePacket(raw)];

  const packets = [];
  let i = 0;
  while (i < raw.length) {
    // raw[i] is always the delimiter opening this segment's length prefix
    // (true for every iteration, not just the first -- the earlier version
    // of this loop only skipped it before the FIRST packet, via the
    // initial `i = 1`, and then read subsequent segments starting ON their
    // own opening delimiter instead of past it, corrupting every packet
    // after the first in a multi-packet payload).
    i++;
    let lengthStr = "";
    while (i < raw.length && raw[i] !== PAYLOAD_DELIMITER) {
      lengthStr += raw[i];
      i++;
    }
    i++; // skip the delimiter that closes the length prefix
    const length = Number(lengthStr);
    packets.push(decodePacket(raw.substr(i, length)));
    i += length;
  }
  return packets;
}
