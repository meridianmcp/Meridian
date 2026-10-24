'use strict';

// Write grants. A write to the library needs a key that a person granted at the desktop. The bearer token admits a
// request, but it does not grant writes, so a process that holds the token cannot write until someone at the desktop
// answers a prompt. Zotero's local API works the same way: it asks the user before it issues a key. Three things differ:
// the default answer is Deny; a grant is never remembered across a restart; and the key goes back once, to the
// request that asked for it.
//
// This module is pure logic. The clock, the random source and the prompt are injected, so the node tests run the
// whole state machine without a timer or a dialog.

var HOUR_MS = 60 * 60 * 1000;
// A session grant runs until Zotero restarts. Zotero's restart clears it. This cap also ends it after this long.
var SESSION_MAX_MS = 8 * HOUR_MS;
var DENY_COOLDOWN_MS = 5 * 60 * 1000;
var REQUEST_WINDOW_MS = 10 * 60 * 1000;
var REQUEST_LIMIT = 6;
var APP_NAME_MAX = 80;
var MAX_RECORDS = 100;
var KEY_HEX_LENGTH = 64;
var ID_HEX_LENGTH = 32;

function createGrants(deps) {
  var v = deps.validate;
  var now = deps.now;
  var randomHex = deps.randomHex;
  var ask = deps.ask;
  var constantTimeEquals = deps.constantTimeEquals;

  if (typeof now !== 'function') throw new Error('createGrants needs deps.now');
  if (typeof randomHex !== 'function') throw new Error('createGrants needs deps.randomHex');
  if (typeof ask !== 'function') throw new Error('createGrants needs deps.ask');
  if (typeof constantTimeEquals !== 'function') throw new Error('createGrants needs deps.constantTimeEquals');

  var records = new Map();
  var pendingId = null;
  var cooldowns = new Map();
  var requestTimes = [];

  function refused(status, code, message) {
    return v.apiError(status, code, message);
  }

  // A grant that has run out is marked expired and loses its key. Nothing else depends on the clock.
  function expireIfDue(record) {
    if (record.status === 'granted' && now() >= record.expiresAt) {
      record.status = 'expired';
      record.key = null;
    }
  }

  // Keeps the table small. Pending records are never dropped, and the oldest finished record goes first.
  function prune() {
    if (records.size <= MAX_RECORDS) return;
    var iterator = records.keys();
    while (records.size > MAX_RECORDS) {
      var next = iterator.next();
      if (next.done) return;
      var record = records.get(next.value);
      if (record && record.status !== 'pending') records.delete(next.value);
    }
  }

  function settle(record, choice) {
    if (pendingId === record.id) pendingId = null;
    if (record.status !== 'pending') return;
    if (choice === 'hour' || choice === 'session') {
      try {
        record.key = randomHex(KEY_HEX_LENGTH / 2);
      } catch (error) {
        record.status = 'denied';
        return;
      }
      record.status = 'granted';
      record.expiresAt = now() + (choice === 'hour' ? HOUR_MS : SESSION_MAX_MS);
      record.choice = choice;
      return;
    }
    record.status = 'denied';
    cooldowns.set(record.app.toLowerCase(), now() + DENY_COOLDOWN_MS);
  }

  // Asks for a grant. Only one request may wait for an answer at a time, so a second request cannot be handed the
  // first one's id, and a prompt cannot pile up. The answer comes back to the request that asked, through status().
  function request(body) {
    v.requireObject(body, 'body');
    var name = v.requireString(body.app_name, 'app_name', APP_NAME_MAX);
    name = name.trim();
    if (name === '' || name.length > APP_NAME_MAX) {
      throw v.invalid('app_name', 'must be 1 to ' + APP_NAME_MAX + ' characters');
    }
    if (pendingId !== null) {
      throw refused(429, 'grant_busy', 'Another request is waiting for an answer at the desktop. Try again when it is answered.');
    }
    var cooldown = cooldowns.get(name.toLowerCase());
    if (cooldown !== undefined && now() < cooldown) {
      throw refused(429, 'grant_cooldown', 'That request was refused at the desktop. Wait a few minutes before asking again.');
    }
    var cutoff = now() - REQUEST_WINDOW_MS;
    requestTimes = requestTimes.filter(function (time) {
      return time > cutoff;
    });
    if (requestTimes.length >= REQUEST_LIMIT) {
      throw refused(429, 'grant_rate_limited', 'Too many grant requests. Wait a few minutes before asking again.');
    }
    requestTimes.push(now());

    var record = {
      id: randomHex(ID_HEX_LENGTH / 2),
      app: name,
      status: 'pending',
      key: null,
      expiresAt: null,
      choice: null,
      delivered: false,
    };
    records.set(record.id, record);
    pendingId = record.id;
    prune();
    Promise.resolve()
      .then(function () {
        return ask(name);
      })
      .then(
        function (choice) {
          settle(record, choice);
        },
        function () {
          // A dialog that cannot be shown is a refusal. Nothing is granted by an error.
          settle(record, 'deny');
        }
      );
    return { grant_id: record.id, status: 'pending' };
  }

  function findRecord(id) {
    if (typeof id !== 'string' || id.length !== ID_HEX_LENGTH || !/^[0-9a-f]+$/.test(id)) {
      throw refused(404, 'not_found', 'No grant has that id.');
    }
    var record = records.get(id);
    if (!record) throw refused(404, 'not_found', 'No grant has that id.');
    expireIfDue(record);
    return record;
  }

  // Reports the state of a grant. The key comes back once, when the grant is first seen as granted.
  function status(id) {
    var record = findRecord(id);
    var out = { grant_id: record.id, status: record.status };
    if (record.status === 'granted') {
      out.expires_at = new Date(record.expiresAt).toISOString();
      if (!record.delivered) {
        out.write_key = record.key;
        record.delivered = true;
      }
    }
    return out;
  }

  // True when the key belongs to a grant that is still granted. The comparison takes the same time whatever the key is.
  function checkKey(key) {
    if (typeof key !== 'string' || key.length !== KEY_HEX_LENGTH) return false;
    var matched = false;
    records.forEach(function (record) {
      expireIfDue(record);
      if (record.status === 'granted' && record.key !== null && constantTimeEquals(record.key, key)) {
        matched = true;
      }
    });
    return matched;
  }

  // Ends a grant at once. The key stops working on the next request.
  function revoke(id) {
    var record = findRecord(id);
    if (record.status === 'pending') {
      if (pendingId === record.id) pendingId = null;
    }
    if (record.status === 'pending' || record.status === 'granted') {
      record.status = 'revoked';
      record.key = null;
    }
    return { grant_id: record.id, status: record.status };
  }

  return {
    request: request,
    status: status,
    checkKey: checkKey,
    revoke: revoke,
  };
}

module.exports = {
  createGrants: createGrants,
  HOUR_MS: HOUR_MS,
  SESSION_MAX_MS: SESSION_MAX_MS,
  DENY_COOLDOWN_MS: DENY_COOLDOWN_MS,
  REQUEST_LIMIT: REQUEST_LIMIT,
};
