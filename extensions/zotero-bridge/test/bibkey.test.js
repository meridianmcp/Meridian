'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { readBibKey, isSafeBibKey, isSafeFileKey, citationKeyFromImport } = require('../lib/bibkey.js');

test('isSafeFileKey refuses a colon and a Windows device stem, because the key becomes a file name', () => {
  assert.equal(isSafeFileKey('doe:2019'), false, 'a colon names an alternate data stream');
  assert.equal(isSafeFileKey('CON'), false);
  assert.equal(isSafeFileKey('aux'), false);
  assert.equal(isSafeFileKey('com1'), false);
  assert.equal(isSafeFileKey('nul.2020'), false, 'the stem before the dot is the device');
  assert.equal(isSafeFileKey('CONIN$'), false);
  assert.equal(isSafeFileKey('smith2020a'), true);
  assert.equal(isSafeFileKey('console2020'), true, 'a device stem must match as a whole');
  assert.equal(isSafeFileKey('../x'), false, 'everything isSafeBibKey refuses is refused here too');
});

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

test('citationKeyFromImport reads itemID, which is where the BibTeX import translator keeps the key', () => {
  assert.equal(citationKeyFromImport({ itemID: 'smith2020a', title: 'T' }), 'smith2020a');
});

test('citationKeyFromImport prefers an explicit citationKey, then a Citation Key line, then itemID', () => {
  assert.equal(citationKeyFromImport({ citationKey: 'explicit', itemID: 'other' }), 'explicit');
  assert.equal(citationKeyFromImport({ extra: 'Citation Key: fromextra', itemID: 'other' }), 'fromextra');
  assert.equal(citationKeyFromImport({ itemID: 'other' }), 'other');
});

test('citationKeyFromImport refuses unsafe, empty, or non-string keys, and missing input', () => {
  assert.equal(citationKeyFromImport({ itemID: '../escape' }), null);
  assert.equal(citationKeyFromImport({ itemID: 42 }), null);
  assert.equal(citationKeyFromImport({ itemID: '' }), null);
  assert.equal(citationKeyFromImport({}), null);
  assert.equal(citationKeyFromImport(null), null);
});

test('a key read from an import can be written as the plugin line and read back for the PDF step', () => {
  const key = citationKeyFromImport({ itemID: 'doe2019_x-y' });
  assert.equal(readBibKey('BibTeX key: ' + key), 'doe2019_x-y');
});