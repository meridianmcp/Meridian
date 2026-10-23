'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const v = require('../lib/api/validate.js');
const auth = require('../lib/api/auth.js');
const { createGrants, HOUR_MS, SESSION_MAX_MS, DENY_COOLDOWN_MS, REQUEST_LIMIT } = require('../lib/api/grants.js');

// Lets the promise chain that answers a request run to the end.
function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

// Builds a store with a fake clock, a counter for random values, and a dialog that answers with `answer`.
function setup(answer) {
  const clock = { t: 1_000_000 };
  let counter = 0;
  const asked = [];
  const store = createGrants({
    validate: v,
    now: () => clock.t,
    randomHex: (bytes) => {
      counter += 1;
      return counter.toString(16).padStart(bytes * 2, '0');
    },
    ask: async (appName) => {
      asked.push(appName);
      if (answer instanceof Error) throw answer;
      return typeof answer === 'function' ? answer(appName) : answer;
    },
    constantTimeEquals: auth.constantTimeEquals,
  });
  return { store, clock, asked };
}

function apiErrorMatch(status, code) {
  return (error) => error.isApiError === true && error.httpStatus === status && error.code === code;
}

// Asks for a grant and waits for the desktop's answer, then returns the id and the answered status.
async function askAndAnswer(store, appName) {
  const { grant_id } = store.request({ app_name: appName });
  await flush();
  return grant_id;
}

test('a granted request returns its key once, and checkKey accepts only that key', async () => {
  const { store } = setup('hour');
  const id = await askAndAnswer(store, 'Claude Code');
  const first = store.status(id);
  assert.equal(first.status, 'granted');
  assert.equal(first.write_key.length, 64);
  assert.equal(store.checkKey(first.write_key), true);
  assert.equal(store.checkKey('0'.repeat(64)), false, 'a wrong key of the right length is refused');
  assert.equal(store.checkKey('abc'), false, 'a key of the wrong length is refused');
  assert.equal(store.checkKey(undefined), false, 'no key is refused');
  const second = store.status(id);
  assert.equal(second.status, 'granted');
  assert.equal(second.write_key, undefined, 'the key is never returned a second time');
});

test('a denied request grants nothing, and the same app waits five minutes before asking again', async () => {
  const { store, clock } = setup('deny');
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(store.status(id).status, 'denied');
  assert.equal(store.status(id).write_key, undefined);
  assert.throws(() => store.request({ app_name: 'claude code' }), apiErrorMatch(429, 'grant_cooldown'), 'the name match ignores case');
  clock.t += DENY_COOLDOWN_MS;
  const retry = await askAndAnswer(store, 'Claude Code');
  assert.notEqual(retry, id);
});

test('only one request waits for an answer, so a second request cannot be handed the first one', async () => {
  let release;
  const { store } = setup(() => new Promise((resolve) => (release = resolve)));
  const first = store.request({ app_name: 'One' });
  await flush();
  assert.throws(() => store.request({ app_name: 'Two' }), apiErrorMatch(429, 'grant_busy'));
  assert.throws(() => store.request({ app_name: 'One' }), apiErrorMatch(429, 'grant_busy'), 'the same name gets no id either');
  release('deny');
  await flush();
  assert.equal(store.status(first.grant_id).status, 'denied');
  store.request({ app_name: 'Two' });
});

test('a dialog that cannot be shown denies, and nothing is granted by an error', async () => {
  const { store } = setup(new Error('no window'));
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(store.status(id).status, 'denied');
});

test('a one-hour grant expires on the clock, and its key stops working', async () => {
  const { store, clock } = setup('hour');
  const id = await askAndAnswer(store, 'Claude Code');
  const { write_key } = store.status(id);
  clock.t += HOUR_MS - 1;
  assert.equal(store.checkKey(write_key), true);
  clock.t += 2;
  assert.equal(store.checkKey(write_key), false);
  assert.equal(store.status(id).status, 'expired');
});

test('a session grant runs for eight hours at most, even though Zotero restarts end it sooner', async () => {
  const { store, clock } = setup('session');
  const id = await askAndAnswer(store, 'Claude Code');
  const { write_key, expires_at } = store.status(id);
  assert.equal(new Date(expires_at).getTime(), 1_000_000 + SESSION_MAX_MS);
  clock.t += SESSION_MAX_MS;
  assert.equal(store.checkKey(write_key), false);
});

test('a revoked grant stops working at once', async () => {
  const { store } = setup('hour');
  const id = await askAndAnswer(store, 'Claude Code');
  const { write_key } = store.status(id);
  assert.equal(store.revoke(id).status, 'revoked');
  assert.equal(store.checkKey(write_key), false);
});

test('a request still waiting can be revoked, and its later answer grants nothing', async () => {
  let release;
  const { store } = setup(() => new Promise((resolve) => (release = resolve)));
  const { grant_id } = store.request({ app_name: 'Claude Code' });
  await flush();
  assert.equal(store.revoke(grant_id).status, 'revoked');
  release('hour');
  await flush();
  assert.equal(store.status(grant_id).status, 'revoked');
  assert.equal(store.status(grant_id).write_key, undefined);
});

test('the number of requests in ten minutes is limited', async () => {
  const { store, clock } = setup('deny');
  for (let i = 0; i < REQUEST_LIMIT; i += 1) {
    await askAndAnswer(store, 'App ' + i);
  }
  assert.throws(() => store.request({ app_name: 'One more' }), apiErrorMatch(429, 'grant_rate_limited'));
  clock.t += 10 * 60 * 1000 + 1;
  await askAndAnswer(store, 'Later');
});

test('a bad app name, or an unknown or malformed grant id, is refused', () => {
  const { store } = setup('deny');
  assert.throws(() => store.request({}), apiErrorMatch(422, 'invalid_value'));
  assert.throws(() => store.request({ app_name: '   ' }), apiErrorMatch(422, 'invalid_value'));
  assert.throws(() => store.request({ app_name: 'x'.repeat(81) }), apiErrorMatch(422, 'invalid_value'));
  assert.throws(() => store.status('0'.repeat(32)), apiErrorMatch(404, 'not_found'));
  assert.throws(() => store.status('not-hex'), apiErrorMatch(404, 'not_found'));
  assert.throws(() => store.revoke('0'.repeat(32)), apiErrorMatch(404, 'not_found'));
});

test('the factory refuses to start without its clock, randomness, dialog and comparison', () => {
  const base = { validate: v, now: () => 0, randomHex: () => '', ask: async () => 'deny', constantTimeEquals: auth.constantTimeEquals };
  assert.throws(() => createGrants({ ...base, now: undefined }), /needs deps\.now/);
  assert.throws(() => createGrants({ ...base, randomHex: undefined }), /needs deps\.randomHex/);
  assert.throws(() => createGrants({ ...base, ask: undefined }), /needs deps\.ask/);
  assert.throws(() => createGrants({ ...base, constantTimeEquals: undefined }), /needs deps\.constantTimeEquals/);
});
