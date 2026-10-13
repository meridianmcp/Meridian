'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { classify, planImport } = require('../lib/duplicates.js');

test('DOI match is exact, even when titles differ', () => {
  const entry = { title: 'A Completely Different Title', doi: '10.1000/xyz' };
  const existing = [{ title: 'Original Title', doi: 'https://doi.org/10.1000/XYZ' }];
  assert.equal(classify(entry, existing), 'exact');
});

test('no DOI on either side and title match is exact', () => {
  const entry = { title: 'Same Title', doi: '' };
  const existing = [{ title: 'same  title.', doi: undefined }];
  assert.equal(classify(entry, existing), 'exact');
});

test('same title with different DOIs is a conflict', () => {
  const entry = { title: 'Shared Title', doi: '10.1000/one' };
  const existing = [{ title: 'Shared Title', doi: '10.1000/two' }];
  assert.equal(classify(entry, existing), 'conflict');
});

test('same title where only the entry has a DOI is a conflict', () => {
  const entry = { title: 'Shared Title', doi: '10.1000/one' };
  const existing = [{ title: 'Shared Title', doi: '' }];
  assert.equal(classify(entry, existing), 'conflict');
});

test('same title where only the existing item has a DOI is a conflict', () => {
  const entry = { title: 'Shared Title', doi: '' };
  const existing = [{ title: 'Shared Title', doi: '10.1000/one' }];
  assert.equal(classify(entry, existing), 'conflict');
});

test('different title is new, even with no DOI on either side', () => {
  const entry = { title: 'Title A', doi: '' };
  const existing = [{ title: 'Title B', doi: '' }];
  assert.equal(classify(entry, existing), 'new');
});

test('different title with different DOI is new', () => {
  const entry = { title: 'Title A', doi: '10.1000/a' };
  const existing = [{ title: 'Title B', doi: '10.1000/b' }];
  assert.equal(classify(entry, existing), 'new');
});

test('empty library means new', () => {
  assert.equal(classify({ title: 'Anything', doi: '10.1/x' }, []), 'new');
  assert.equal(classify({ title: 'Anything', doi: '' }, undefined), 'new');
});

test('entries without a title never match on title', () => {
  const existing = [{ title: '', doi: '' }, { title: undefined, doi: '' }];
  assert.equal(classify({ title: '', doi: '' }, existing), 'new');
});

test('an exact match takes precedence over a conflicting title match', () => {
  const entry = { title: 'Shared Title', doi: '' };
  const existing = [
    { title: 'Shared Title', doi: '10.1000/b' },
    { title: 'Shared Title', doi: '' },
  ];
  assert.equal(classify(entry, existing), 'exact');
});

test('planImport separates imports, exact duplicates and conflicts', () => {
  const existing = [
    { title: 'Known Paper', doi: '10.1000/known' },
    { title: 'Clash Paper', doi: '10.1000/clash-old' },
  ];
  const entries = [
    { title: 'Known Paper renamed', doi: 'https://doi.org/10.1000/KNOWN' },
    { title: 'Clash Paper', doi: '10.1000/clash-new' },
    { title: 'Fresh Paper', doi: '10.1000/fresh' },
  ];
  const plan = planImport(entries, existing);
  assert.deepEqual(plan.exact.map((e) => e.title), ['Known Paper renamed']);
  assert.deepEqual(plan.conflicts.map((e) => e.title), ['Clash Paper']);
  assert.deepEqual(plan.toImport.map((e) => e.title), ['Fresh Paper']);
});

test('planImport treats a second identical entry in the same file as exact', () => {
  const entries = [
    { title: 'Twice In File', doi: '10.1000/twice' },
    { title: 'Twice in file', doi: '10.1000/TWICE' },
  ];
  const plan = planImport(entries, []);
  assert.equal(plan.toImport.length, 1);
  assert.equal(plan.exact.length, 1);
  assert.equal(plan.conflicts.length, 0);
});

test('planImport does not mutate the existing list it was given', () => {
  const existing = [{ title: 'Keep', doi: '' }];
  planImport([{ title: 'Added', doi: '' }], existing);
  assert.equal(existing.length, 1);
});
