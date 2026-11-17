'use strict';

// Request admission for the local write API: Host, Origin, and bearer token.
// Pure logic. The caller passes the expected Host value and the token, so this
// file never names an address.

var TOKEN_SHAPE_RE = /^[A-Za-z0-9_-]+$/;
var BEARER_RE = /^Bearer ([^ \t]+)$/i;
var MIN_TOKEN_LENGTH = 32;

// The token file holds one line. Accept only a value that looks like a random
// URL-safe secret of a useful length, so an empty or mangled file never starts the API.
function isUsableToken(token) {
  return typeof token === 'string' && token.length >= MIN_TOKEN_LENGTH && TOKEN_SHAPE_RE.test(token);
}

// Compares every character, so the time taken does not reveal where two strings first differ.
function constantTimeEquals(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  var diff = a.length ^ b.length;
  var length = Math.max(a.length, b.length);
  for (var i = 0; i < length; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

// Returns null when the request is admitted, or { httpStatus, code, message }.
// Order matters: Host first (DNS rebinding), then Origin (browser pages), then the token.
function admit(headers, expected) {
  var host = headers.host;
  if (typeof host !== 'string' || host.toLowerCase() !== String(expected.host).toLowerCase()) {
    return { httpStatus: 403, code: 'host_not_allowed', message: 'The Host header must name this API exactly.' };
  }
  if ('origin' in headers) {
    return { httpStatus: 403, code: 'origin_not_allowed', message: 'Requests from a browser page are not accepted.' };
  }
  var auth = typeof headers.authorization === 'string' ? BEARER_RE.exec(headers.authorization) : null;
  if (!auth || !constantTimeEquals(auth[1], expected.token)) {
    return { httpStatus: 401, code: 'unauthorized', message: 'Send Authorization: Bearer <token> from the Meridian token file.' };
  }
  return null;
}

module.exports = {
  isUsableToken: isUsableToken,
  constantTimeEquals: constantTimeEquals,
  admit: admit,
};
