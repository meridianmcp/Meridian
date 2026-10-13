'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { readBibKey, isSafeBibKey } = require('../lib/bibkey.js');

test('reads the BibTeX key line from a multi-line Extra field', () => {
  const extra = 'Original Date: 2020-01-01\nBibTeX key: smith2020a\nPMID: 123';
  assert.equal(readBibKey(extra), 'smith2020a');
});

test('tolerates CRLF line endings and surrounding spaces', () => {
  assert.equal(readBibKey('Foo: bar\r\nBibTeX key:   doe:2019-x_y  \r\n'), 'doe:2019-x_y');
});

test('returns null when the line is missing or Extra is not a string', () => {
  assert.equal(readBibKey('Citation Key: other'), null);
  assert.equal(readBibKey(''), null);
  assert.equal(readBibKey(undefined), null);
  assert.equal(readBibKey(null), null);
});

test('returns null when the key line has extra tokens after the key', () => {
  assert.equal(readBibKey('BibTeX key: one two'), null);
});

test('rejects path-like or unsafe keys', () => {
  assert.equal(readBibKey('BibTeX key: ../secret'), null);
  assert.equal(readBibKey('BibTeX key: sub/dir'), null);
  assert.equal(readBibKey('BibTeX key: sub\\dir'), null);
  assert.equal(readBibKey('BibTeX key: .hidden'), null);
  assert.equal(readBibKey('BibTeX key: a..b/c'), null);
});

test('isSafeBibKey accepts ordinary BibTeX keys', () => {
  assert.equal(isSafeBibKey('smith2020a'), true);
  assert.equal(isSafeBibKey('doe:2019-x_y.z+1'), true);
});

test('isSafeBibKey rejects traversal, separators, spaces, leading dots and overlong keys', () => {
  assert.equal(isSafeBibKey('../x'), false);
  assert.equal(isSafeBibKey('a/b'), false);
  assert.equal(isSafeBibKey('a\\b'), false);
  assert.equal(isSafeBibKey('.x'), false);
  assert.equal(isSafeBibKey('a b'), false);
  assert.equal(isSafeBibKey(''), false);
  assert.equal(isSafeBibKey('a'.repeat(201)), false);
  assert.equal(isSafeBibKey(null), false);
});
