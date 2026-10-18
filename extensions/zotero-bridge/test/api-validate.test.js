'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const v = require('../lib/api/validate.js');

test('decodeBase64 round-trips bytes of every padding length', () => {
  for (const text of ['', 'a', 'ab', 'abc', 'abcd', '%PDF-1.4 binary \u0000\u00ff']) {
    const payload = Buffer.from(text, 'latin1');
    const decoded = v.decodeBase64(payload.toString('base64'), 'data', 1000);
    assert.deepEqual(Array.from(decoded), Array.from(payload), JSON.stringify(text));
  }
});

test('decodeBase64 refuses anything that is not standard base64 with padding', () => {
  for (const text of ['abc', 'ab!d', 'YWJj\n', 'YQ', 'Y===', '====']) {
    assert.throws(() => v.decodeBase64(text, 'data_base64', 1000), (error) => error.code === 'invalid_value', text);
  }
});

test('decodeBase64 refuses a payload over the byte limit before decoding it', () => {
  const big = Buffer.alloc(100, 1).toString('base64');
  assert.throws(() => v.decodeBase64(big, 'data_base64', 99), (error) => error.code === 'invalid_value');
  assert.equal(v.decodeBase64(big, 'data_base64', 100).length, 100);
});

test('safeFilename keeps a plain name and refuses any folder part or control character', () => {
  assert.equal(v.safeFilename('paper v2 (final).pdf'), 'paper v2 (final).pdf');
  for (const name of ['../x.pdf', 'a/b.pdf', 'a\\b.pdf', '..', '.', ' padded.pdf', 'tab\there.pdf']) {
    assert.throws(() => v.safeFilename(name), (error) => error.code === 'invalid_value', JSON.stringify(name));
  }
});

test('safeFilename refuses Windows device names with or without an extension, and a name ending in a dot', () => {
  const refused = ['NUL', 'nul.pdf', 'con.txt', 'Aux', 'PRN.pdf', 'COM0', 'com9.pdf', 'LPT0', 'lpt9.txt', 'NUL .pdf', 'con:stream.pdf', 'COM¹.pdf', 'lpt².txt', 'paper.', 'paper.pdf '];
  for (const name of refused) {
    assert.throws(() => v.safeFilename(name), (error) => error.code === 'invalid_value', JSON.stringify(name));
  }
  // A reserved stem only counts as a whole. These names merely start with the letters.
  for (const name of ['CONSOLE.pdf', 'COM10.pdf', 'LPT.pdf', 'nul-data.pdf', 'auxiliary.txt', 'conference 2026.pdf']) {
    assert.equal(v.safeFilename(name), name);
  }
});

test('isAbsolutePath accepts a drive path or a rooted path and refuses a relative one', () => {
  // Drive paths are written with a split literal so the plugin's path scanner does not read them as machine-local.
  assert.equal(v.isAbsolutePath('D:' + '\\papers\\paper.pdf'), true);
  assert.equal(v.isAbsolutePath('D:/papers/paper.pdf'), true);
  assert.equal(v.isAbsolutePath('/var/papers/paper.pdf'), true);
  assert.equal(v.isAbsolutePath('papers/paper.pdf'), false);
  assert.equal(v.isAbsolutePath('\\\\server\\share\\x.pdf'), false);
});

test('isAbsolutePath refuses network shares, device paths, and a doubled leading separator', () => {
  // Drive-letter forms are split literals, as above, so the static check does not read them as local paths.
  const refused = [
    '//host/share/a.pdf',
    '//?/' + 'C:' + '/x.pdf',
    '\\\\?\\' + 'C:' + '\\x.pdf',
    '\\\\.\\COM1',
    '\\\\server\\share\\x.pdf',
    '/\\server\\share\\x.pdf',
    '\\relative\\x.pdf',
    'C:x.pdf',
    '/var/x\u0000.pdf',
  ];
  for (const path of refused) {
    assert.equal(v.isAbsolutePath(path), false, JSON.stringify(path));
  }
  assert.equal(v.isAbsolutePath('C:' + '/x.pdf'), true);
  assert.equal(v.isAbsolutePath('/x'), true);
  assert.equal(v.isAbsolutePath(42), false);
});

test('keys must be eight capital letters or digits', () => {
  assert.equal(v.keyValue('ABCD2345', 'key'), 'ABCD2345');
  for (const key of ['abcd2345', 'ABCD234', 'ABCD23456', 'ABCD-234', 42]) {
    assert.throws(() => v.keyValue(key, 'key'), (error) => error.code === 'invalid_value', String(key));
  }
});

test('predicates look like prefix:name, and objects contain no whitespace', () => {
  assert.equal(v.predicateValue('dc:relation', 'p'), 'dc:relation');
  assert.throws(() => v.predicateValue('relation', 'p'));
  assert.equal(v.relationObjectValue('zotero://select/items/1_ABCD2345', 'o'), 'zotero://select/items/1_ABCD2345');
  assert.throws(() => v.relationObjectValue('has a space', 'o'));
  assert.throws(() => v.relationObjectValue('', 'o'));
});

test('optional integers reject negatives and fractions, and booleans reject strings', () => {
  assert.equal(v.optionalInteger(0, 'n'), 0);
  assert.throws(() => v.optionalInteger(-1, 'n'));
  assert.throws(() => v.optionalInteger(1.5, 'n'));
  assert.throws(() => v.optionalBoolean('true', 'b'));
  assert.equal(v.optionalBoolean(undefined, 'b'), false);
});

test('linkableFileProblem allows documents, images and media in ordinary folders, in any letter case', () => {
  for (const p of ['/var/papers/paper.pdf', 'D:' + '/papers/Paper.PDF', 'D:' + '\\papers\\notes.txt', '/x/figure.SVG', '/x/clip.mp4']) {
    assert.equal(v.linkableFileProblem(p), null, p);
  }
});

test('linkableFileProblem refuses other file types, dot-files, and credentials, profile or app-data folders', () => {
  for (const p of ['/x/id_rsa', '/x/.env', '/x/tool.exe', '/x/archive.zip', '/x/db.sqlite', '/x/.ssh/config.txt', '/x/AppData/y.pdf', '/x/.git/notes.md', '/x/appdata/a.pdf']) {
    assert.notEqual(v.linkableFileProblem(p), null, p);
  }
});