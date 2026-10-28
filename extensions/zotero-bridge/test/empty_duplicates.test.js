'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { findEmptyDuplicates } = require('../lib/empty_duplicates.js');

const empty = { title: '', doi: '', hasChildren: false };

test('of two empty copies with the same BibTeX key, the older is kept and the newer is listed for the trash', () => {
  const found = findEmptyDuplicates([
    { ...empty, id: 2, key: 'smith2020', dateAdded: '2026-01-02 10:00:00' },
    { ...empty, id: 1, key: 'smith2020', dateAdded: '2026-01-01 10:00:00' },
  ]);
  assert.deepEqual(found.trash, [{ id: 2, key: 'smith2020' }]);
  assert.deepEqual(found.skipped, []);
});

test('items with a title or a DOI are never candidates, even when they share a key', () => {
  const found = findEmptyDuplicates([
    { ...empty, id: 1, key: 'k', title: 'A Real Paper' },
    { ...empty, id: 2, key: 'k', title: 'A Real Paper' },
    { ...empty, id: 3, key: 'k', doi: '10.1000/x' },
    { ...empty, id: 4, key: 'k', doi: '10.1000/x' },
  ]);
  assert.deepEqual(found.trash, []);
});

test('a single empty item is not a duplicate', () => {
  assert.deepEqual(findEmptyDuplicates([{ ...empty, id: 1, key: 'k' }]).trash, []);
});

test('items without a usable BibTeX key are never grouped', () => {
  const found = findEmptyDuplicates([
    { ...empty, id: 1, key: null },
    { ...empty, id: 2, key: '' },
    { ...empty, id: 3 },
    { ...empty, id: 4, key: 'not a key' },
    { ...empty, id: 5, key: 'not a key' },
  ]);
  assert.deepEqual(found.trash, []);
});

test('a later copy with attachments or notes is kept and reported as skipped', () => {
  const found = findEmptyDuplicates([
    { ...empty, id: 1, key: 'k', dateAdded: '2026-01-01 00:00:00' },
    { ...empty, id: 2, key: 'k', dateAdded: '2026-01-02 00:00:00', hasChildren: true },
    { ...empty, id: 3, key: 'k', dateAdded: '2026-01-03 00:00:00' },
  ]);
  assert.deepEqual(found.trash, [{ id: 3, key: 'k' }]);
  assert.deepEqual(found.skipped, [{ id: 2, key: 'k', reason: 'has attachments or notes' }]);
});

test('the oldest copy is kept even when it has children, and the other copies are still trashed', () => {
  const found = findEmptyDuplicates([
    { ...empty, id: 1, key: 'k', dateAdded: '2026-01-01 00:00:00', hasChildren: true },
    { ...empty, id: 2, key: 'k', dateAdded: '2026-01-02 00:00:00' },
  ]);
  assert.deepEqual(found.trash, [{ id: 2, key: 'k' }]);
  assert.deepEqual(found.skipped, []);
});

test('ties on dateAdded fall back to id order, so the lower id is kept', () => {
  const found = findEmptyDuplicates([
    { ...empty, id: 9, key: 'k', dateAdded: '2026-01-01 00:00:00' },
    { ...empty, id: 4, key: 'k', dateAdded: '2026-01-01 00:00:00' },
  ]);
  assert.deepEqual(found.trash, [{ id: 9, key: 'k' }]);
});

test('different keys form separate groups', () => {
  const found = findEmptyDuplicates([
    { ...empty, id: 1, key: 'a', dateAdded: '2026-01-01 00:00:00' },
    { ...empty, id: 2, key: 'a', dateAdded: '2026-01-02 00:00:00' },
    { ...empty, id: 3, key: 'b', dateAdded: '2026-01-01 00:00:00' },
    { ...empty, id: 4, key: 'b', dateAdded: '2026-01-02 00:00:00' },
  ]);
  assert.deepEqual(found.trash, [{ id: 2, key: 'a' }, { id: 4, key: 'b' }]);
});

test('a missing list returns no candidates', () => {
  assert.deepEqual(findEmptyDuplicates(undefined), { trash: [], skipped: [] });
});
