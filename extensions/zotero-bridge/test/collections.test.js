'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { pickCollectionByName } = require('../lib/collections.js');

const A = { id: 1, name: 'Reading' };
const B = { id: 2, name: 'reading' };
const C = { id: 3, name: 'Reading' };
const D = { id: 4, name: 'Other' };

test('finds a single collection by exact name', () => {
  const result = pickCollectionByName([A, D], 'Reading');
  assert.equal(result.status, 'ok');
  assert.equal(result.collection, A);
});

test('name matching is exact and case-sensitive', () => {
  assert.equal(pickCollectionByName([B], 'Reading').status, 'none');
  assert.equal(pickCollectionByName([A], ' Reading').status, 'none');
});

test('no match returns none', () => {
  assert.equal(pickCollectionByName([D], 'Reading').status, 'none');
  assert.equal(pickCollectionByName([], 'Reading').status, 'none');
});

test('empty or non-string name returns none', () => {
  assert.equal(pickCollectionByName([A], '').status, 'none');
  assert.equal(pickCollectionByName([A], undefined).status, 'none');
});

test('several matches prefer the collection the menu was opened on', () => {
  const result = pickCollectionByName([A, C, D], 'Reading', 3);
  assert.equal(result.status, 'ok');
  assert.equal(result.collection, C);
});

test('several matches with no preferred id is ambiguous', () => {
  const result = pickCollectionByName([A, C], 'Reading', 99);
  assert.equal(result.status, 'ambiguous');
  assert.equal(result.matches.length, 2);
});
