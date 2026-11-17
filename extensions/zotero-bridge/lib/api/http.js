'use strict';

// HTTP/1.1 request parsing and response building for the local write API.
// Pure JavaScript: no sockets, no Zotero globals. node --test covers every branch.
// The socket code that feeds this parser lives in api/listener.js.

var MAX_HEAD_BYTES = 16 * 1024;
// Holds a base64 attachment at its 24 MiB decoded limit, plus the JSON around it.
var MAX_BODY_BYTES = 40 * 1024 * 1024;
var METHOD_RE = /^[A-Z]{3,7}$/;
var TARGET_RE = /^\/[\x21-\x7e]*$/;
var HEADER_NAME_RE = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
var CONTENT_LENGTH_RE = /^[0-9]{1,10}$/;
var SINGLE_VALUE_HEADERS = ['content-length', 'host', 'authorization', 'origin', 'content-type'];

var REASONS = {
  200: 'OK',
  201: 'Created',
  400: 'Bad Request',
  401: 'Unauthorized',
  403: 'Forbidden',
  404: 'Not Found',
  405: 'Method Not Allowed',
  409: 'Conflict',
  413: 'Payload Too Large',
  415: 'Unsupported Media Type',
  422: 'Unprocessable Entity',
  431: 'Request Header Fields Too Large',
  500: 'Internal Server Error',
  501: 'Not Implemented',
  505: 'HTTP Version Not Supported',
};

function parseFailure(httpStatus, code, message) {
  return { status: 'error', httpStatus: httpStatus, code: code, message: message };
}

function concatBytes(first, second) {
  var out = new Uint8Array(first.length + second.length);
  out.set(first, 0);
  out.set(second, first.length);
  return out;
}

function findHeadEnd(bytes) {
  for (var i = 0; i + 3 < bytes.length; i++) {
    if (bytes[i] === 13 && bytes[i + 1] === 10 && bytes[i + 2] === 13 && bytes[i + 3] === 10) return i;
  }
  return -1;
}

// Headers are ASCII. Latin-1 keeps every byte as one character, so an odd byte
// in a header cannot throw. The header check rejects anything outside the token set.
function latin1(bytes) {
  var text = '';
  for (var i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
  return text;
}

function parseHead(text) {
  var lines = text.split('\r\n');
  var requestLine = /^([A-Z]+) (\/[\x21-\x7e]*) HTTP\/(\d\.\d)$/.exec(lines[0]);
  if (!requestLine) return parseFailure(400, 'bad_request_line', 'The request line must be METHOD /path HTTP/1.1.');
  if (requestLine[3] !== '1.1') return parseFailure(505, 'http_version', 'Only HTTP/1.1 is supported.');
  if (!METHOD_RE.test(requestLine[1])) return parseFailure(400, 'bad_method', 'The method is not valid.');
  if (!TARGET_RE.test(requestLine[2])) return parseFailure(400, 'bad_target', 'The request target is not valid.');

  var headers = Object.create(null);
  for (var i = 1; i < lines.length; i++) {
    var line = lines[i];
    if (/^[ \t]/.test(line)) return parseFailure(400, 'obsolete_folding', 'Folded header lines are not accepted.');
    var colon = line.indexOf(':');
    if (colon <= 0) return parseFailure(400, 'bad_header', 'A header line has no name.');
    var name = line.slice(0, colon);
    if (!HEADER_NAME_RE.test(name)) return parseFailure(400, 'bad_header', 'A header name contains an invalid character.');
    name = name.toLowerCase();
    var value = line.slice(colon + 1).replace(/^[ \t]+|[ \t]+$/g, '');
    if (name in headers) {
      if (SINGLE_VALUE_HEADERS.indexOf(name) !== -1) {
        return parseFailure(400, 'duplicate_header', 'The ' + name + ' header appears more than once.');
      }
      headers[name] = headers[name] + ', ' + value;
    } else {
      headers[name] = value;
    }
  }
  if ('transfer-encoding' in headers) {
    return parseFailure(501, 'transfer_encoding', 'Send a Content-Length body. Chunked bodies are not supported.');
  }
  return {
    status: 'head',
    method: requestLine[1],
    target: requestLine[2],
    headers: headers,
  };
}

function bodyLength(headers, maxBody) {
  if (!('content-length' in headers)) return 0;
  var raw = headers['content-length'];
  if (!CONTENT_LENGTH_RE.test(raw)) {
    return parseFailure(400, 'bad_content_length', 'Content-Length must be a whole number of bytes.');
  }
  var length = parseInt(raw, 10);
  if (length > maxBody) {
    return parseFailure(413, 'payload_too_large', 'The request body is larger than ' + maxBody + ' bytes.');
  }
  return length;
}

// Feed bytes as they arrive. The parser answers in two steps, so the caller can refuse a request before
// its body is read. Each push returns one of:
//   { status: 'incomplete' }                       more bytes are needed
//   { status: 'head', method, target, headers, length }
//                                                  the header block is complete. Returned once. Body bytes
//                                                  that came with it are kept here. Push again, with new
//                                                  bytes or an empty Uint8Array, to go on.
//   { status: 'complete', method, target, headers, body: Uint8Array }
//   { status: 'error', httpStatus, code, message }
// A parser that returned 'complete' or 'error' is finished. Connections close after one request.
function createRequestParser(options) {
  var maxHead = (options && options.maxHeadBytes) || MAX_HEAD_BYTES;
  var maxBody = (options && options.maxBodyBytes) || MAX_BODY_BYTES;
  var head = new Uint8Array(0);
  var pending = null;
  var bodyChunks = [];
  var bodyReceived = 0;
  var finished = false;

  // Copies the bytes, so the caller may reuse its buffer.
  function keepBody(bytes) {
    if (bytes.length === 0) return;
    bodyChunks.push(bytes.slice(0));
    bodyReceived += bytes.length;
  }

  function takeBody() {
    var body = new Uint8Array(bodyReceived);
    var offset = 0;
    bodyChunks.forEach(function (chunk) {
      body.set(chunk, offset);
      offset += chunk.length;
    });
    return body;
  }

  return {
    push: function (bytes) {
      if (finished) return parseFailure(500, 'parser_reused', 'The parser already finished a request.');
      if (!pending) {
        head = concatBytes(head, bytes);
        var end = findHeadEnd(head);
        if (end < 0) {
          if (head.length > maxHead) {
            finished = true;
            return parseFailure(431, 'head_too_large', 'Request headers are larger than ' + maxHead + ' bytes.');
          }
          return { status: 'incomplete' };
        }
        if (end > maxHead) {
          finished = true;
          return parseFailure(431, 'head_too_large', 'Request headers are larger than ' + maxHead + ' bytes.');
        }
        var parsed = parseHead(latin1(head.subarray(0, end)));
        if (parsed.status === 'error') {
          finished = true;
          return parsed;
        }
        var length = bodyLength(parsed.headers, maxBody);
        if (typeof length !== 'number') {
          finished = true;
          return length;
        }
        pending = { method: parsed.method, target: parsed.target, headers: parsed.headers, length: length };
        keepBody(head.subarray(end + 4));
        head = null;
        // Stop here, so the caller can admit or refuse the request before any body is read.
        return {
          status: 'head',
          method: pending.method,
          target: pending.target,
          headers: pending.headers,
          length: pending.length,
        };
      }
      keepBody(bytes);
      if (bodyReceived > pending.length) {
        finished = true;
        return parseFailure(400, 'extra_bytes', 'More bytes arrived than Content-Length allows.');
      }
      if (bodyReceived < pending.length) return { status: 'incomplete' };
      finished = true;
      return {
        status: 'complete',
        method: pending.method,
        target: pending.target,
        headers: pending.headers,
        body: takeBody(),
      };
    },
  };
}

function utf8Encode(text) {
  if (typeof TextEncoder === 'function') return new TextEncoder().encode(text);
  var encoded = encodeURIComponent(text);
  var bytes = [];
  for (var i = 0; i < encoded.length; i++) {
    if (encoded.charAt(i) === '%') {
      bytes.push(parseInt(encoded.substr(i + 1, 2), 16));
      i += 2;
    } else {
      bytes.push(encoded.charCodeAt(i));
    }
  }
  return Uint8Array.from(bytes);
}

// Throws a URIError on bytes that are not valid UTF-8.
function utf8Decode(bytes) {
  if (typeof TextDecoder === 'function') return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  var percent = '';
  for (var i = 0; i < bytes.length; i++) {
    percent += '%' + (bytes[i] < 16 ? '0' : '') + bytes[i].toString(16);
  }
  return decodeURIComponent(percent);
}

// Builds the bytes of a complete response. Every response closes the connection,
// says it must not be cached, and sends no CORS headers.
function buildResponse(status, payload, extraHeaders) {
  var body = utf8Encode(JSON.stringify(payload));
  var lines = [
    'HTTP/1.1 ' + status + ' ' + (REASONS[status] || 'Status'),
    'Content-Type: application/json; charset=utf-8',
    'Content-Length: ' + body.length,
    'Cache-Control: no-store',
    'Connection: close',
    'X-Content-Type-Options: nosniff',
  ];
  (extraHeaders || []).forEach(function (line) {
    lines.push(line);
  });
  var head = utf8Encode(lines.join('\r\n') + '\r\n\r\n');
  return { status: status, bytes: concatBytes(head, body) };
}

module.exports = {
  MAX_HEAD_BYTES: MAX_HEAD_BYTES,
  MAX_BODY_BYTES: MAX_BODY_BYTES,
  createRequestParser: createRequestParser,
  buildResponse: buildResponse,
  utf8Encode: utf8Encode,
  utf8Decode: utf8Decode,
  REASONS: REASONS,
};
