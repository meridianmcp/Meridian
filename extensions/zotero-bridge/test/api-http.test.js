'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('../lib/api/http.js');
const auth = require('../lib/api/auth.js');
const v = require('../lib/api/validate.js');

function bytes(text) {
  return http.utf8Encode(text);
}

// Feeds chunks the way the listener does. A 'head' result is followed by an empty push, so a body-less
// request completes at once, and a request with a body keeps going on the next chunk.
function feed(parser, chunks) {
  let state = { status: 'incomplete' };
  for (const chunk of chunks) {
    state = advance(parser, typeof chunk === 'string' ? bytes(chunk) : chunk);
    if (state.status === 'complete' || state.status === 'error') return state;
  }
  return state;
}

function advance(parser, chunk) {
  const state = parser.push(chunk);
  return state.status === 'head' ? parser.push(new Uint8Array(0)) : state;
}

test('a GET with no body parses to a complete request with lower-cased headers', () => {
  const parser = http.createRequestParser();
  const state = feed(parser, ['GET /v1/status HTTP/1.1\r\nHost: 127.0.0.1:23125\r\nAuthorization: Bearer abc\r\n\r\n']);
  assert.equal(state.status, 'complete');
  assert.equal(state.method, 'GET');
  assert.equal(state.target, '/v1/status');
  assert.equal(state.headers.host, '127.0.0.1:23125');
  assert.equal(state.headers.authorization, 'Bearer abc');
  assert.equal(state.body.length, 0);
});

test('a head split across many small chunks still parses', () => {
  const text = 'POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: 2\r\n\r\n{}';
  const chunks = text.split('').map((ch) => ch);
  const state = feed(http.createRequestParser(), chunks);
  assert.equal(state.status, 'complete');
  assert.equal(new TextDecoder().decode(state.body), '{}');
});

test('a body split across chunks is joined byte-for-byte, including multi-byte UTF-8', () => {
  const body = '{"title":"Café — 研究"}';
  const encoded = bytes(body);
  const head = bytes(`POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: ${encoded.length}\r\n\r\n`);
  const mid = Math.floor(encoded.length / 2);
  const state = feed(http.createRequestParser(), [head, encoded.slice(0, mid), encoded.slice(mid)]);
  assert.equal(state.status, 'complete');
  assert.deepEqual(Array.from(state.body), Array.from(encoded));
  assert.equal(http.utf8Decode(state.body), body);
});

test('a chunked body is refused with 501, not guessed at', () => {
  const state = feed(http.createRequestParser(), ['POST /v1/items HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\n\r\n']);
  assert.equal(state.status, 'error');
  assert.equal(state.httpStatus, 501);
});

test('a Content-Length that is not a plain number is refused', () => {
  for (const value of ['-1', '1e3', '12abc', ' ', '']) {
    const state = feed(http.createRequestParser(), [`POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: ${value}\r\n\r\n`]);
    assert.equal(state.status, 'error', `Content-Length "${value}" must be refused`);
    assert.equal(state.httpStatus, 400);
  }
});

test('a body over the limit is refused from the header, before any body arrives', () => {
  const parser = http.createRequestParser({ maxBodyBytes: 10 });
  const state = feed(parser, ['POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: 11\r\n\r\n']);
  assert.equal(state.status, 'error');
  assert.equal(state.httpStatus, 413);
});

test('headers over the limit are refused with 431 and the parser stops', () => {
  const parser = http.createRequestParser({ maxHeadBytes: 64 });
  const state = feed(parser, ['GET / HTTP/1.1\r\nX-Padding: ' + 'a'.repeat(200)]);
  assert.equal(state.status, 'error');
  assert.equal(state.httpStatus, 431);
});

test('a repeated Host header is refused, since the Host check depends on one value', () => {
  const state = feed(http.createRequestParser(), ['GET / HTTP/1.1\r\nHost: 127.0.0.1:23125\r\nHost: evil.example\r\n\r\n']);
  assert.equal(state.status, 'error');
  assert.equal(state.httpStatus, 400);
  assert.equal(state.code, 'duplicate_header');
});

test('folded header lines, bad names, HTTP/1.0 and bad request lines are refused', () => {
  const cases = [
    ['GET / HTTP/1.1\r\nHost: h\r\n folded\r\n\r\n', 400, 'obsolete_folding'],
    ['GET / HTTP/1.1\r\nBad Name: x\r\n\r\n', 400, 'bad_header'],
    ['GET / HTTP/1.0\r\nHost: h\r\n\r\n', 505, 'http_version'],
    ['GET HTTP/1.1\r\nHost: h\r\n\r\n', 400, 'bad_request_line'],
    ['get / HTTP/1.1\r\nHost: h\r\n\r\n', 400, 'bad_request_line'],
  ];
  for (const [text, status, code] of cases) {
    const state = feed(http.createRequestParser(), [text]);
    assert.equal(state.status, 'error', text);
    assert.equal(state.httpStatus, status, text);
    assert.equal(state.code, code, text);
  }
});

test('bytes past the declared body are refused, so one request cannot smuggle a second', () => {
  const state = feed(http.createRequestParser(), ['POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: 2\r\n\r\n{}GET / HTTP/1.1\r\n\r\n']);
  assert.equal(state.status, 'error');
  assert.equal(state.code, 'extra_bytes');
});

test('a finished parser refuses more input', () => {
  const parser = http.createRequestParser();
  feed(parser, ['GET / HTTP/1.1\r\nHost: h\r\n\r\n']);
  assert.equal(parser.push(bytes('x')).status, 'error');
});

test('the head is reported once, before the body arrives, and the body completes on later pushes', () => {
  const parser = http.createRequestParser();
  const first = parser.push(bytes('POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n\r\n'));
  assert.equal(first.status, 'head');
  assert.equal(first.method, 'POST');
  assert.equal(first.target, '/v1/items');
  assert.equal(first.headers.host, 'h');
  assert.equal(first.length, 5);
  assert.equal(parser.push(new Uint8Array(0)).status, 'incomplete', 'the head is not repeated');
  assert.equal(parser.push(bytes('hel')).status, 'incomplete');
  const done = parser.push(bytes('lo'));
  assert.equal(done.status, 'complete');
  assert.equal(http.utf8Decode(done.body), 'hello');
});

test('body bytes that arrive with the head wait in the parser, and an empty push completes the request', () => {
  const parser = http.createRequestParser();
  const first = parser.push(bytes('POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: 2\r\n\r\n{}'));
  assert.equal(first.status, 'head');
  const done = parser.push(new Uint8Array(0));
  assert.equal(done.status, 'complete');
  assert.equal(http.utf8Decode(done.body), '{}');
});

test('a request with no body is complete on the push that follows its head', () => {
  const parser = http.createRequestParser();
  assert.equal(parser.push(bytes('GET /v1/status HTTP/1.1\r\nHost: h\r\n\r\n')).status, 'head');
  const done = parser.push(new Uint8Array(0));
  assert.equal(done.status, 'complete');
  assert.equal(done.body.length, 0);
});

test('a body over the limit is refused on the head push, so no body is awaited', () => {
  const parser = http.createRequestParser({ maxBodyBytes: 10 });
  const first = parser.push(bytes('POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: 11\r\n\r\n'));
  assert.equal(first.status, 'error');
  assert.equal(first.httpStatus, 413);
});

test('bytes past the declared body are refused on the push after the head', () => {
  const parser = http.createRequestParser();
  const first = parser.push(bytes('POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: 2\r\n\r\n{}GET / HTTP/1.1\r\n\r\n'));
  assert.equal(first.status, 'head');
  const state = parser.push(new Uint8Array(0));
  assert.equal(state.status, 'error');
  assert.equal(state.code, 'extra_bytes');
});

test('the body cap is 40 MiB, and a base64 attachment at its decoded limit still fits with room for its JSON', () => {
  assert.equal(http.MAX_BODY_BYTES, 40 * 1024 * 1024);
  // Same bound that lib/api/items.js applies to data_base64.
  const base64Length = Math.ceil((v.LIMITS.attachmentBytes * 4) / 3) + 4;
  assert.ok(base64Length + 1024 * 1024 <= http.MAX_BODY_BYTES);
  const atCap = http.createRequestParser().push(bytes(`POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: ${http.MAX_BODY_BYTES}\r\n\r\n`));
  assert.equal(atCap.status, 'head');
  const overCap = http.createRequestParser().push(bytes(`POST /v1/items HTTP/1.1\r\nHost: h\r\nContent-Length: ${http.MAX_BODY_BYTES + 1}\r\n\r\n`));
  assert.equal(overCap.httpStatus, 413);
});

test('responses carry an exact Content-Length, JSON UTF-8, and no CORS headers', () => {
  const response = http.buildResponse(200, { title: 'Café — 研究' });
  const text = new TextDecoder().decode(response.bytes);
  const [head, body] = text.split('\r\n\r\n');
  assert.match(head, /^HTTP\/1\.1 200 OK\r\n/);
  assert.match(head, /Content-Type: application\/json; charset=utf-8/);
  assert.match(head, /Connection: close/);
  assert.match(head, /Cache-Control: no-store/);
  assert.equal(/access-control/i.test(head), false, 'no CORS headers');
  const declared = Number(/Content-Length: (\d+)/.exec(head)[1]);
  assert.equal(declared, bytes(body).length);
  assert.deepEqual(JSON.parse(body), { title: 'Café — 研究' });
});

test('utf8Decode rejects bytes that are not valid UTF-8', () => {
  assert.throws(() => http.utf8Decode(Uint8Array.from([0xff, 0xfe, 0x00])));
});

test('UTF-8 round-trips without TextEncoder and TextDecoder, as in a Zotero sandbox that lacks them', () => {
  // Zotero's add-on sandbox may not provide these globals, so the fallback has to be right on its own.
  const saved = { encoder: globalThis.TextEncoder, decoder: globalThis.TextDecoder };
  delete globalThis.TextEncoder;
  delete globalThis.TextDecoder;
  try {
    const text = 'Café — 研究 🎓 "quoted" \\back';
    const bytes = http.utf8Encode(text);
    assert.deepEqual(Array.from(bytes), Array.from(Buffer.from(text, 'utf8')));
    assert.equal(http.utf8Decode(bytes), text);
    assert.throws(() => http.utf8Decode(Uint8Array.from([0xc3])), 'a truncated sequence is refused');
  } finally {
    globalThis.TextEncoder = saved.encoder;
    globalThis.TextDecoder = saved.decoder;
  }
});

test('auth: only a usable random-looking token passes the shape check', () => {
  assert.equal(auth.isUsableToken(''), false);
  assert.equal(auth.isUsableToken('short'), false);
  assert.equal(auth.isUsableToken('a'.repeat(31)), false);
  assert.equal(auth.isUsableToken('has a space ' + 'a'.repeat(40)), false);
  assert.equal(auth.isUsableToken('path/like/' + 'a'.repeat(40)), false);
  assert.equal(auth.isUsableToken('abcDEF_012-' + 'x'.repeat(30)), true);
});

test('auth: constant-time comparison matches ordinary equality', () => {
  assert.equal(auth.constantTimeEquals('abc', 'abc'), true);
  assert.equal(auth.constantTimeEquals('abc', 'abd'), false);
  assert.equal(auth.constantTimeEquals('abc', 'abcd'), false);
  assert.equal(auth.constantTimeEquals('', ''), true);
  assert.equal(auth.constantTimeEquals(undefined, 'abc'), false);
});

test('auth: Host is checked first, then Origin, then the bearer token', () => {
  const expected = { host: '127.0.0.1:23125', token: 'T'.repeat(40) };
  const good = { host: '127.0.0.1:23125', authorization: 'Bearer ' + 'T'.repeat(40) };
  assert.equal(auth.admit(good, expected), null);
  assert.equal(auth.admit({ ...good, host: '127.0.0.1:23125'.toUpperCase().toLowerCase() }, expected), null);
  assert.equal(auth.admit({ ...good, host: 'evil.example:23125' }, expected).httpStatus, 403);
  assert.equal(auth.admit({ ...good, host: 'localhost:23125' }, expected).code, 'host_not_allowed');
  assert.equal(auth.admit({ ...good, origin: 'https://example.com' }, expected).code, 'origin_not_allowed');
  assert.equal(auth.admit({ host: good.host }, expected).httpStatus, 401);
  assert.equal(auth.admit({ ...good, authorization: 'Bearer ' + 'X'.repeat(40) }, expected).httpStatus, 401);
  assert.equal(auth.admit({ ...good, authorization: 'Basic ' + 'T'.repeat(40) }, expected).httpStatus, 401);
  assert.equal(auth.admit({ ...good, authorization: 'bearer ' + 'T'.repeat(40) }, expected), null);
});
