'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeTitle, normalizeDoi } = require('../lib/normalize.js');

test('normalizeTitle lowercases and collapses whitespace', () => {
  assert.equal(normalizeTitle('  A   Study\tof\nDNA  '), 'a study of dna');
});

test('normalizeTitle trims leading and trailing punctuation', () => {
  assert.equal(normalizeTitle('"Quoted Title."'), 'quoted title');
  assert.equal(normalizeTitle('...Ellipsis...'), 'ellipsis');
});

test('normalizeTitle keeps internal punctuation', () => {
  assert.equal(normalizeTitle('Foo: a bar'), 'foo: a bar');
});

test('normalizeTitle strips BibTeX brace protection', () => {
  assert.equal(normalizeTitle('{DNA} repair in {E. coli}'), 'dna repair in e. coli');
});

test('normalizeTitle returns empty string for non-strings and blanks', () => {
  assert.equal(normalizeTitle(undefined), '');
  assert.equal(normalizeTitle(null), '');
  assert.equal(normalizeTitle(42), '');
  assert.equal(normalizeTitle('   '), '');
  assert.equal(normalizeTitle('...'), '');
});

test('normalizeDoi lowercases and strips doi.org prefixes', () => {
  assert.equal(normalizeDoi('10.1000/ABC.def'), '10.1000/abc.def');
  assert.equal(normalizeDoi('https://doi.org/10.1000/ABC'), '10.1000/abc');
  assert.equal(normalizeDoi('http://dx.doi.org/10.1000/abc'), '10.1000/abc');
  assert.equal(normalizeDoi('doi:10.1000/abc'), '10.1000/abc');
});

test('normalizeDoi trims whitespace and returns empty string when missing', () => {
  assert.equal(normalizeDoi('  10.1000/abc  '), '10.1000/abc');
  assert.equal(normalizeDoi(''), '');
  assert.equal(normalizeDoi(undefined), '');
  assert.equal(normalizeDoi(null), '');
});
