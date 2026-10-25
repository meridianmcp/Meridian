'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const v = require('../lib/api/validate.js');
const auth = require('../lib/api/auth.js');
const { createGrants, HOUR_MS, STANDING_MS, SESSION_MAX_MS, DENY_COOLDOWN_MS, REQUEST_LIMIT } = require('../lib/api/grants.js');

// Lets the promise chain that answers a request run to the end.
function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

// Stands in for the preference the desktop saves a standing approval in. The store reads it once at startup and
// writes it when a standing approval is made or cleared. `writes` records every value that was saved.
function fakeStanding(initial = null) {
  const standing = {
    value: initial,
    writes: [],
    read: () => standing.value,
    write: (value) => {
      standing.writes.push(value);
      standing.value = value;
    },
  };
  return standing;
}

// Builds a store with a fake clock, a counter for random values, and a dialog that answers with `answer`.
function setup(answer, options = {}) {
  const clock = { t: 1_000_000 };
  let counter = 0;
  const asked = [];
  const standing = options.standing || fakeStanding();
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
    standing,
  });
  return { store, clock, asked, standing };
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

test('the factory refuses to start without the standing store', () => {
  const base = { validate: v, now: () => 0, randomHex: () => '', ask: async () => 'deny', constantTimeEquals: auth.constantTimeEquals };
  assert.throws(() => createGrants(base), /needs deps\.standing/);
});

test('a standing approval grants the next request at once, with no dialog', async () => {
  const { store, asked, standing } = setup('standing');
  const first = await askAndAnswer(store, 'Claude Code');
  assert.equal(store.status(first).status, 'granted');
  assert.equal(standing.value, 1_000_000 + STANDING_MS, 'the approval is saved until 30 days on');
  const next = store.request({ app_name: 'Another app' });
  assert.equal(next.status, 'granted', 'the answer comes back at once, with no wait');
  assert.deepEqual(asked, ['Claude Code'], 'the second request never reached the dialog');
  const granted = store.status(next.grant_id);
  assert.equal(granted.status, 'granted');
  assert.equal(granted.write_key.length, 64);
  assert.equal(store.checkKey(granted.write_key), true);
  assert.equal(new Date(granted.expires_at).getTime(), 1_000_000 + HOUR_MS, 'the key a standing approval gives is a one-hour key');
});

test('a standing approval lasts 30 days, and the request after that prompts again', async () => {
  const answers = ['standing', 'hour'];
  const { store, clock, asked } = setup(() => answers.shift());
  await askAndAnswer(store, 'Claude Code');
  clock.t += STANDING_MS - 1;
  assert.equal(store.request({ app_name: 'Another app' }).status, 'granted', 'still in force one millisecond before the end');
  clock.t += 1;
  await askAndAnswer(store, 'Third app');
  assert.equal(asked.length, 2, 'at 30 days the request goes to the dialog again');
});

test('an expired standing approval is ignored: the next request prompts, and the saved expiry is cleared', async () => {
  const answers = ['standing', 'deny'];
  const { store, clock, asked, standing } = setup(() => answers.shift());
  await askAndAnswer(store, 'Claude Code');
  clock.t += STANDING_MS;
  const id = await askAndAnswer(store, 'Another app');
  assert.equal(asked.length, 2, 'the request after expiry prompts');
  assert.equal(store.status(id).status, 'denied');
  assert.equal(standing.value, null, 'the stored expiry is cleared once it has run out');
});

test('a standing approval saved before this start is read at startup and honoured', () => {
  const standing = fakeStanding(1_000_000 + STANDING_MS - 1);
  const { store, asked } = setup('deny', { standing });
  const out = store.request({ app_name: 'Claude Code' });
  assert.equal(out.status, 'granted');
  assert.deepEqual(asked, [], 'no dialog');
});

test('a standing approval that ran out before this start is ignored, and the request prompts', async () => {
  const standing = fakeStanding(1_000_000 - 1);
  const { store, asked } = setup('deny', { standing });
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(asked.length, 1, 'the request prompts');
  assert.equal(store.status(id).status, 'denied');
});

test('a saved expiry more than 30 days ahead is not trusted, so the request prompts', async () => {
  // No standing approval can run past 30 days from now, so a value further off was not written by one. It grants nothing.
  const standing = fakeStanding(1_000_000 + STANDING_MS + 1);
  const { store, asked } = setup('deny', { standing });
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(asked.length, 1, 'the request prompts');
  assert.equal(store.status(id).status, 'denied');
});

test('revoking the standing approval clears it, and the next request prompts again', async () => {
  const answers = ['standing', 'hour'];
  const { store, asked, standing } = setup(() => answers.shift());
  assert.deepEqual(store.revokeStanding(), { standing: 'none' }, 'clearing with none in force is harmless');
  await askAndAnswer(store, 'Claude Code');
  assert.deepEqual(store.revokeStanding(), { standing: 'none' });
  assert.equal(standing.value, null, 'the saved approval is cleared');
  const id = await askAndAnswer(store, 'Another app');
  assert.equal(asked.length, 2, 'the next request prompts');
  assert.equal(store.status(id).status, 'granted');
});

test('deny refuses, and creates no standing approval', async () => {
  const { store, asked, standing } = setup('deny');
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(store.status(id).status, 'denied');
  assert.equal(standing.value, null);
  assert.deepEqual(standing.writes, [], 'nothing was saved');
  store.request({ app_name: 'Another app' });
  await flush();
  assert.equal(asked.length, 2, 'the next request still goes to the dialog');
});

test('a request body cannot create or extend a standing approval', async () => {
  const { store, asked, standing } = setup('hour');
  const extra = { choice: 'standing', standing: true, allow_standing: true, standing_until: 9e15, expires_at: 9e15 };
  const first = store.request({ app_name: 'Claude Code', ...extra });
  await flush();
  assert.equal(store.status(first.grant_id).status, 'granted');
  assert.equal(standing.value, null, 'the one-hour answer saved nothing, whatever the body said');
  assert.deepEqual(standing.writes, []);
  store.request({ app_name: 'Another app', ...extra });
  await flush();
  assert.equal(asked.length, 2, 'the next request with the same body still prompts');
});

test('a standing approval does not skip the cooldown or the request limit', async () => {
  const answers = ['deny', 'standing'];
  const { store, asked, standing } = setup(() => answers.shift());
  await askAndAnswer(store, 'Claude Code');
  await askAndAnswer(store, 'Other app');
  assert.notEqual(standing.value, null, 'the second app made a standing approval');
  assert.throws(() => store.request({ app_name: 'Claude Code' }), apiErrorMatch(429, 'grant_cooldown'));
  for (let i = 0; i < REQUEST_LIMIT - 2; i += 1) {
    assert.equal(store.request({ app_name: 'App ' + i }).status, 'granted');
  }
  assert.throws(() => store.request({ app_name: 'One more' }), apiErrorMatch(429, 'grant_rate_limited'));
  assert.equal(asked.length, 2, 'only the two dialog requests prompted');
});

test('if the standing approval cannot be saved, the request still gets its one-hour key and no standing approval is made', async () => {
  const answers = ['standing', 'standing'];
  const standing = fakeStanding();
  standing.write = () => {
    throw new Error('preference cannot be written');
  };
  const { store, asked } = setup(() => answers.shift(), { standing });
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(store.status(id).status, 'granted', 'the approval for this request stands');
  assert.equal(standing.value, null, 'nothing was kept, so nothing is in force');
  await askAndAnswer(store, 'Another app');
  assert.equal(asked.length, 2, 'with no saved approval, the next request prompts');
});

test('a standing answer whose key cannot be made grants nothing and saves no standing approval', async () => {
  const standing = fakeStanding();
  let calls = 0;
  const store = createGrants({
    validate: v,
    now: () => 1_000_000,
    randomHex: (bytes) => {
      calls += 1;
      if (calls > 1) throw new Error('no secure random source');
      return '1'.repeat(bytes * 2);
    },
    ask: async () => 'standing',
    constantTimeEquals: auth.constantTimeEquals,
    standing,
  });
  const id = await askAndAnswer(store, 'Claude Code');
  assert.equal(store.status(id).status, 'denied');
  assert.deepEqual(standing.writes, []);
});

test('the one-hour and until-restart choices are unchanged, and neither saves a standing approval', async () => {
  const hour = setup('hour');
  const hourId = await askAndAnswer(hour.store, 'Claude Code');
  assert.equal(new Date(hour.store.status(hourId).expires_at).getTime(), 1_000_000 + HOUR_MS);
  const session = setup('session');
  const sessionId = await askAndAnswer(session.store, 'Claude Code');
  assert.equal(new Date(session.store.status(sessionId).expires_at).getTime(), 1_000_000 + SESSION_MAX_MS);
  assert.deepEqual(hour.standing.writes, []);
  assert.deepEqual(session.standing.writes, []);
});
