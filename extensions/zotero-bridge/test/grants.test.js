'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const v = require('../lib/api/validate.js');
const auth = require('../lib/api/auth.js');
const { createGrants, promptChoice, HOUR_MS, SESSION_MAX_MS, DENY_COOLDOWN_MS, REQUEST_LIMIT } = require('../lib/api/grants.js');
const { createRemembered } = require('../lib/api/remembered.js');

// Lets the promise chain that answers a request run to the end.
function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

// Builds a store with a fake clock, a counter for random values, a dialog that answers with `answer`, and a remembered
// list kept in one in-memory preference. `options.failWrites` makes the preference refuse every write.
function setup(answer, options = {}) {
  const clock = { t: 1_000_000 };
  let counter = 0;
  const asked = [];
  const pref = { text: '[]' };
  const logged = [];
  const remembered = createRemembered({
    read: () => pref.text,
    write: (text) => {
      if (options.failWrites) throw new Error('pref locked');
      pref.text = text;
    },
    now: () => clock.t,
  });
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
    remembered,
    log: (error) => logged.push(error),
  });
  return { store, clock, asked, pref, remembered, logged };
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

test('the factory refuses to start without its clock, randomness, dialog, comparison and remembered list', () => {
  const remembered = { has: () => false, add: () => {}, remove: () => 0 };
  const base = {
    validate: v,
    now: () => 0,
    randomHex: () => '',
    ask: async () => 'deny',
    constantTimeEquals: auth.constantTimeEquals,
    remembered,
  };
  assert.throws(() => createGrants({ ...base, now: undefined }), /needs deps\.now/);
  assert.throws(() => createGrants({ ...base, randomHex: undefined }), /needs deps\.randomHex/);
  assert.throws(() => createGrants({ ...base, ask: undefined }), /needs deps\.ask/);
  assert.throws(() => createGrants({ ...base, constantTimeEquals: undefined }), /needs deps\.constantTimeEquals/);
  assert.throws(() => createGrants({ ...base, remembered: undefined }), /needs deps\.remembered/);
  assert.throws(() => createGrants({ ...base, remembered: { has: () => false } }), /needs deps\.remembered/);
});

test('the desktop answer grants only the three allow buttons, and everything else refuses', () => {
  assert.equal(promptChoice(0), 'hour', 'Allow for 1 hour');
  assert.equal(promptChoice(2), 'session', 'Allow until Zotero restarts');
  assert.equal(promptChoice(3), 'remember', 'Allow for this app, remembered');
  assert.equal(promptChoice(1), 'deny', 'Deny is the second button, which Escape comes back as');
  for (const answer of [undefined, null, -1, 4, '0', '3', true]) {
    assert.equal(promptChoice(answer), 'deny', `answer ${String(answer)} refuses`);
  }
});

test('remember stores the app name with the time approved, and grants for an hour', async () => {
  const { store, asked, pref, remembered } = setup('remember');
  const id = await askAndAnswer(store, 'Claude Code');
  const first = store.status(id);
  assert.equal(first.status, 'granted');
  assert.equal(first.write_key.length, 64);
  assert.equal(new Date(first.expires_at).getTime(), 1_000_000 + HOUR_MS);
  assert.equal(store.checkKey(first.write_key), true);
  assert.equal(remembered.has('Claude Code'), true);
  const stored = JSON.parse(pref.text);
  assert.equal(stored.length, 1);
  assert.equal(stored[0].name, 'Claude Code');
  assert.equal(stored[0].approved_at, new Date(1_000_000).toISOString());
  assert.deepEqual(asked, ['Claude Code'], 'the dialog was shown once, for the first request');
});

test('a remembered name is granted without a prompt, in any letter case, and at once', async () => {
  const { store, asked, clock } = setup('remember');
  await askAndAnswer(store, 'Claude Code');
  clock.t += 1000;
  const { grant_id, status } = store.request({ app_name: '  claude code ' });
  assert.equal(status, 'granted', 'the grant is made at once, with no wait for an answer');
  assert.deepEqual(asked, ['Claude Code'], 'the dialog was not shown again');
  const out = store.status(grant_id);
  assert.equal(out.status, 'granted');
  assert.equal(out.write_key.length, 64);
  assert.equal(store.checkKey(out.write_key), true);
  assert.equal(new Date(out.expires_at).getTime(), 1_000_000 + 1000 + HOUR_MS);
  assert.equal(store.status(grant_id).write_key, undefined, 'the key still comes back once');
});

test('a remembered name is granted while a prompt for another app is still waiting', async () => {
  let release;
  const { store, asked, remembered } = setup((name) =>
    name === 'Claude Code' ? 'remember' : new Promise((resolve) => (release = resolve))
  );
  await askAndAnswer(store, 'Claude Code');
  const other = store.request({ app_name: 'Other App' });
  await flush();
  assert.equal(store.request({ app_name: 'Claude Code' }).status, 'granted');
  assert.throws(() => store.request({ app_name: 'Third App' }), apiErrorMatch(429, 'grant_busy'), 'a new name still waits');
  release('deny');
  await flush();
  assert.equal(store.status(other.grant_id).status, 'denied');
  assert.equal(remembered.has('Other App'), false);
  assert.deepEqual(asked, ['Claude Code', 'Other App']);
});

test('a new app name still prompts while another app is remembered', async () => {
  const { store, asked, remembered } = setup((name) => (name === 'Claude Code' ? 'remember' : 'deny'));
  await askAndAnswer(store, 'Claude Code');
  const id = await askAndAnswer(store, 'Other App');
  assert.deepEqual(asked, ['Claude Code', 'Other App']);
  assert.equal(store.status(id).status, 'denied');
  assert.equal(remembered.has('Other App'), false);
});

test('deny refuses, does not remember, and the name waits out the cooldown', async () => {
  const { store, asked, remembered, pref, clock } = setup('deny');
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(store.status(id).status, 'denied');
  assert.equal(remembered.has('Claude Code'), false);
  assert.equal(pref.text, '[]', 'nothing was written to the remembered list');
  assert.throws(() => store.request({ app_name: 'Claude Code' }), apiErrorMatch(429, 'grant_cooldown'));
  clock.t += DENY_COOLDOWN_MS;
  await askAndAnswer(store, 'Claude Code');
  assert.equal(asked.length, 2, 'after the cooldown, the name prompts again');
  assert.equal(remembered.has('Claude Code'), false);
});

test('forgetting a remembered app removes the name and ends each of its live grants', async () => {
  const { store, asked, remembered, pref } = setup('remember');
  const id = await askAndAnswer(store, 'Claude Code');
  const firstKey = store.status(id).write_key;
  const second = store.request({ app_name: 'Claude Code' }).grant_id;
  const secondKey = store.status(second).write_key;
  assert.equal(store.checkKey(firstKey), true);
  assert.equal(store.checkKey(secondKey), true);

  assert.deepEqual(store.forgetApp('claude code'), { removed: 1, ended: 2 });

  assert.equal(store.checkKey(firstKey), false, 'the first grant stops working at once');
  assert.equal(store.checkKey(secondKey), false, 'the grant from the remembered request stops working too');
  assert.equal(store.status(id).status, 'revoked');
  assert.equal(remembered.has('Claude Code'), false);
  assert.equal(JSON.parse(pref.text).length, 0);

  await askAndAnswer(store, 'Claude Code');
  assert.equal(asked.length, 2, 'a forgotten name prompts again');
});

test('forgetting a name that is not remembered still ends its grant, and removes nothing', async () => {
  const { store, remembered } = setup('hour');
  const id = await askAndAnswer(store, 'Claude Code');
  const { write_key } = store.status(id);
  assert.deepEqual(store.forgetApp('Claude Code'), { removed: 0, ended: 1 });
  assert.equal(store.checkKey(write_key), false);
  assert.equal(remembered.has('Claude Code'), false);
});

test('forgetting an app name ends its pending request, so its later answer grants nothing', async () => {
  let release;
  const { store, remembered } = setup(() => new Promise((resolve) => (release = resolve)));
  const { grant_id } = store.request({ app_name: 'Claude Code' });
  await flush();
  assert.deepEqual(store.forgetApp('CLAUDE CODE'), { removed: 0, ended: 1 });
  release('remember');
  await flush();
  assert.equal(store.status(grant_id).status, 'revoked');
  assert.equal(store.status(grant_id).write_key, undefined);
  assert.equal(remembered.has('Claude Code'), false, 'a revoked request does not remember its name');
});

test('forgetting with no usable name changes nothing', () => {
  const { store } = setup('deny');
  assert.deepEqual(store.forgetApp(undefined), { removed: 0, ended: 0 });
  assert.deepEqual(store.forgetApp('   '), { removed: 0, ended: 0 });
});

test('a remember answer that cannot be saved still grants this request, and logs the error', async () => {
  const { store, remembered, logged } = setup('remember', { failWrites: true });
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(store.status(id).status, 'granted');
  assert.equal(remembered.has('Claude Code'), false, 'the name is not remembered');
  assert.equal(logged.length, 1);
  assert.match(logged[0].message, /pref locked/);
});

test('a remembered name counts toward the request limit, so it cannot be used to flood the table', async () => {
  const { store } = setup('remember');
  await askAndAnswer(store, 'Claude Code');
  for (let i = 0; i < REQUEST_LIMIT - 1; i += 1) {
    store.request({ app_name: 'Claude Code' });
  }
  assert.throws(() => store.request({ app_name: 'Claude Code' }), apiErrorMatch(429, 'grant_rate_limited'));
});
