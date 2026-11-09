'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createRemembered, MAX_REMEMBERED, NAME_MAX } = require('../lib/api/remembered.js');

// A store over one in-memory preference. `state.failRead` and `state.failWrite` make the preference refuse.
function setup(initial = '[]') {
  const state = { text: initial, writes: 0, failRead: false, failWrite: false, now: 1_700_000_000_000 };
  const store = createRemembered({
    read: () => {
      if (state.failRead) throw new Error('pref unreadable');
      return state.text;
    },
    write: (text) => {
      if (state.failWrite) throw new Error('pref locked');
      state.writes += 1;
      state.text = text;
    },
    now: () => state.now,
  });
  return { store, state };
}

test('add stores each name with the time it was approved, as a JSON list', () => {
  const { store, state } = setup();
  assert.equal(store.add('Claude Code'), true);
  state.now += 60_000;
  assert.equal(store.add('  Zotero Helper  '), true, 'surrounding spaces are not stored');
  const stored = JSON.parse(state.text);
  assert.deepEqual(stored, [
    { name: 'Claude Code', approved_at: new Date(1_700_000_000_000).toISOString() },
    { name: 'Zotero Helper', approved_at: new Date(1_700_000_060_000).toISOString() },
  ]);
  assert.deepEqual(store.list(), stored, 'list gives the same entries back');
});

test('adding a name that is already there, in any case, keeps the first approval time', () => {
  const { store, state } = setup();
  store.add('Claude Code');
  const before = state.text;
  state.now += 60_000;
  assert.equal(store.add('claude code'), false);
  assert.equal(state.text, before, 'nothing was rewritten');
  assert.equal(store.list().length, 1);
});

test('has matches a remembered name without regard to case or surrounding spaces', () => {
  const { store } = setup();
  store.add('Claude Code');
  assert.equal(store.has('Claude Code'), true);
  assert.equal(store.has('  CLAUDE code '), true);
  assert.equal(store.has('Claude'), false, 'a part of a name is not the name');
  assert.equal(store.has(''), false);
  assert.equal(store.has('   '), false);
  assert.equal(store.has(undefined), false);
  assert.equal(store.has(42), false);
});

test('remove deletes every entry that matches, and writes only when something changed', () => {
  const { store, state } = setup();
  store.add('Claude Code');
  store.add('Other');
  assert.equal(store.remove('CLAUDE CODE'), 1);
  assert.equal(store.has('Claude Code'), false);
  assert.equal(store.has('Other'), true);
  const writes = state.writes;
  assert.equal(store.remove('Never Added'), 0);
  assert.equal(store.remove(''), 0);
  assert.equal(state.writes, writes, 'a remove that changes nothing does not write');
});

test('text that is not a valid list reads as an empty list, and never throws', () => {
  for (const text of ['', '   ', 'not json', '{"name":"x"}', '42', 'null']) {
    const { store } = setup(text);
    assert.deepEqual(store.list(), [], `text ${JSON.stringify(text)} reads as empty`);
    assert.equal(store.has('x'), false);
  }
});

test('damaged entries are dropped, and the rest of the list is kept', () => {
  const long = 'x'.repeat(NAME_MAX + 1);
  const text = JSON.stringify([
    null,
    'a bare string',
    { name: '' },
    { name: '   ' },
    { name: long },
    { approved_at: '2026-01-01T00:00:00.000Z' },
    { name: 'Kept', approved_at: 'not a date' },
    { name: 'kept' },
    { name: 'Also kept', approved_at: '2026-01-01T00:00:00.000Z' },
  ]);
  const { store } = setup(text);
  assert.deepEqual(store.list(), [
    { name: 'Kept', approved_at: '' },
    { name: 'Also kept', approved_at: '2026-01-01T00:00:00.000Z' },
  ]);
});

test('the list holds at most MAX_REMEMBERED names, and add refuses a name past that', () => {
  const { store } = setup();
  for (let i = 0; i < MAX_REMEMBERED; i += 1) {
    assert.equal(store.add('App ' + i), true);
  }
  assert.throws(() => store.add('One more'), /remembered list is full/);
  assert.equal(store.list().length, MAX_REMEMBERED);
  assert.equal(store.has('One more'), false);
});

test('a stored list longer than the cap is read only up to the cap', () => {
  const entries = [];
  for (let i = 0; i < MAX_REMEMBERED + 5; i += 1) entries.push({ name: 'App ' + i });
  const { store } = setup(JSON.stringify(entries));
  assert.equal(store.list().length, MAX_REMEMBERED);
});

test('add refuses a name that is empty or longer than the limit, and writes nothing', () => {
  const { store, state } = setup();
  assert.throws(() => store.add(''), /must be 1 to 80 characters/);
  assert.throws(() => store.add('x'.repeat(NAME_MAX + 1)), /must be 1 to 80 characters/);
  assert.equal(state.writes, 0);
});

test('a preference that cannot be read reads as empty, and add or remove then refuse to overwrite it', () => {
  const { store, state } = setup('[{"name":"Kept"}]');
  state.failRead = true;
  assert.deepEqual(store.list(), []);
  assert.equal(store.has('Kept'), false);
  assert.throws(() => store.add('New'), /pref unreadable/);
  assert.throws(() => store.remove('Kept'), /pref unreadable/);
  assert.equal(state.writes, 0, 'nothing was written over the unread list');
});

test('a preference that cannot be written makes add throw and leaves the list as it was', () => {
  const { store, state } = setup();
  store.add('Kept');
  const before = state.text;
  state.failWrite = true;
  assert.throws(() => store.add('New'), /pref locked/);
  assert.equal(state.text, before);
});

test('the factory refuses to start without its read, write and clock', () => {
  const base = { read: () => '[]', write: () => {}, now: () => 0 };
  assert.throws(() => createRemembered({ ...base, read: undefined }), /needs deps\.read/);
  assert.throws(() => createRemembered({ ...base, write: undefined }), /needs deps\.write/);
  assert.throws(() => createRemembered({ ...base, now: undefined }), /needs deps\.now/);
});
