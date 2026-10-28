'use strict';

// An entry with no title and no DOI used to be imported on every run, because it could never match on title.
// The BibTeX key the plugin writes to Extra is the only identity such an entry has, so it matches on that.

const test = require('node:test');
const assert = require('node:assert/strict');
const { classify, planImport } = require('../lib/duplicates.js');

test('an untitled entry with no DOI matches an existing item with the same BibTeX key', () => {
  const entry = { title: '', doi: '', key: 'smith2020' };
  const existing = [{ title: '', doi: '', key: 'smith2020' }];
  assert.equal(classify(entry, existing), 'exact');
});

test('an untitled entry with a different BibTeX key is new', () => {
  const entry = { title: '', doi: '', key: 'jones2021' };
  const existing = [{ title: '', doi: '', key: 'smith2020' }];
  assert.equal(classify(entry, existing), 'new');
});

test('an untitled entry with no BibTeX key is still new', () => {
  const entry = { title: '', doi: '' };
  const existing = [{ title: '', doi: '' }];
  assert.equal(classify(entry, existing), 'new');
});

test('an untitled entry that has a DOI is matched by DOI only, not by key', () => {
  const entry = { title: '', doi: '10.1000/one', key: 'smith2020' };
  const existing = [{ title: '', doi: '10.1000/two', key: 'smith2020' }];
  assert.equal(classify(entry, existing), 'new');
});

test('re-importing an untitled entry already in the library imports nothing new', () => {
  const plan = planImport(
    [{ title: '', doi: '', key: 'smith2020' }],
    [{ title: '', doi: '', key: 'smith2020' }],
  );
  assert.equal(plan.toImport.length, 0);
  assert.equal(plan.exact.length, 1);
});

test('a repeated untitled entry in one file imports once', () => {
  const plan = planImport(
    [{ title: '', doi: '', key: 'smith2020' }, { title: '', doi: '', key: 'smith2020' }],
    [],
  );
  assert.equal(plan.toImport.length, 1);
  assert.equal(plan.exact.length, 1);
});
