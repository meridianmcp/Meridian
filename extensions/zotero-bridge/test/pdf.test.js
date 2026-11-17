'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { isPdfHeader, hasPdfHeader } = require('../lib/pdf.js');

function tempFileWith(contents) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'zbridge-pdf-'));
  const file = path.join(dir, 'sample.pdf');
  fs.writeFileSync(file, contents);
  return { dir, file };
}

test('isPdfHeader accepts the %PDF- magic bytes', () => {
  assert.equal(isPdfHeader(Buffer.from('%PDF-1.7\n%...')), true);
  assert.equal(isPdfHeader(new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d])), true);
});

test('isPdfHeader rejects other content and short input', () => {
  assert.equal(isPdfHeader(Buffer.from('%PS-Adobe-3.0')), false);
  assert.equal(isPdfHeader(Buffer.from('PK\x03\x04')), false);
  assert.equal(isPdfHeader(Buffer.from('%PDF')), false);
  assert.equal(isPdfHeader(Buffer.from('%pdf-')), false);
  assert.equal(isPdfHeader(undefined), false);
});

test('hasPdfHeader reads only the first five bytes of a real PDF-like file', () => {
  const { dir, file } = tempFileWith('%PDF-1.4\n' + 'x'.repeat(1000));
  try {
    assert.equal(hasPdfHeader(file), true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('hasPdfHeader is false for a file that is not a PDF', () => {
  const { dir, file } = tempFileWith('<html>not a pdf</html>');
  try {
    assert.equal(hasPdfHeader(file), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('hasPdfHeader is false for an empty file and for a missing file', () => {
  const { dir, file } = tempFileWith('');
  try {
    assert.equal(hasPdfHeader(file), false);
    assert.equal(hasPdfHeader(path.join(dir, 'missing.pdf')), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
