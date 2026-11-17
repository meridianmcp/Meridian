'use strict';

// A stand-in for the Win32 resolver in bootstrap.js. A test declares what each path resolves to, so the link
// policy can be driven through every branch without touching the disk. A path nobody declared is missing.
function createFakeResolver() {
  const entries = new Map();

  async function resolve(path) {
    if (entries.has(path)) return entries.get(path);
    return { status: 'missing' };
  }

  // Declares a path that exists. By default it resolves to itself, with one name, not a link, and not remote.
  resolve.add = function (path, overrides) {
    entries.set(path, Object.assign({ status: 'ok', path: path, leafReparse: false, hardLinks: 1, remote: false }, overrides || {}));
    return resolve;
  };
  // Declares a path that the OS cannot check, such as an access error.
  resolve.fail = function (path) {
    entries.set(path, { status: 'error' });
    return resolve;
  };
  return resolve;
}

module.exports = { createFakeResolver: createFakeResolver };
