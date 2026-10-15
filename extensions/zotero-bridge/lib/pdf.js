'use strict';

// PDF header check. isPdfHeader is pure and used by the Zotero glue, which
// reads the first bytes through IOUtils. hasPdfHeader uses Node's fs and is
// used by the tests only; it reads just the first five bytes of the file.

const PDF_MAGIC = [0x25, 0x50, 0x44, 0x46, 0x2d]; // "%PDF-"

function isPdfHeader(bytes) {
  if (!bytes || bytes.length < PDF_MAGIC.length) return false;
  for (let i = 0; i < PDF_MAGIC.length; i++) {
    if (bytes[i] !== PDF_MAGIC[i]) return false;
  }
  return true;
}

function hasPdfHeader(filePath) {
  const fs = require('fs');
  let fd;
  try {
    fd = fs.openSync(filePath, 'r');
    const buffer = Buffer.alloc(PDF_MAGIC.length);
    const read = fs.readSync(fd, buffer, 0, PDF_MAGIC.length, 0);
    return isPdfHeader(buffer.subarray(0, read));
  } catch (error) {
    return false;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

module.exports = { isPdfHeader, hasPdfHeader };
