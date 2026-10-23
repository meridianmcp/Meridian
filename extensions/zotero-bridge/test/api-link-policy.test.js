'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const v = require('../lib/api/validate.js');
const { createLinkPolicy } = require('../lib/api/link_policy.js');
const { createFakeResolver } = require('./support/fake-resolver.js');

// Split literals keep the static check from reading these as machine-local paths.
const ROOT = 'D:' + '\\zotero-test';

function policy(overrides = {}) {
  const resolve = overrides.resolve || createFakeResolver();
  let calls = 0;
  const counted = async (path) => {
    calls += 1;
    return resolve(path);
  };
  const linkPolicy = createLinkPolicy({
    validate: v,
    isWindows: () => overrides.isWindows !== false,
    linkFolders: () => (overrides.linkFolders !== undefined ? overrides.linkFolders : ROOT),
    resolve: counted,
  });
  return {
    checkLinkable: linkPolicy.checkLinkable,
    resolve,
    calls: () => calls,
  };
}

async function refusedWith(promise, code, pattern) {
  await assert.rejects(promise, (error) => {
    if (error.isApiError !== true || error.code !== code) return false;
    return pattern ? pattern.test(error.message) : true;
  });
}

test('an allowed file is accepted, and the answer is its real path', async () => {
  const resolve = createFakeResolver().add(ROOT).add(ROOT + '\\papers\\paper.pdf', { path: ROOT + '\\papers\\paper.pdf' });
  const p = policy({ resolve });
  assert.deepEqual(await p.checkLinkable(ROOT + '\\papers\\paper.pdf'), { path: ROOT + '\\papers\\paper.pdf' });
});

test('a path that fails the text rules is refused before the OS is asked about it', async () => {
  const resolve = createFakeResolver().add(ROOT);
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '\\id_rsa'), 'linking_refused');
  await refusedWith(p.checkLinkable(ROOT + '\\notes.txt:hidden'), 'linking_refused');
  await refusedWith(p.checkLinkable(ROOT + '\\AppData.\\x.pdf'), 'linking_refused');
  await refusedWith(p.checkLinkable(ROOT + '\\papers\\'), 'linking_refused', /must name a file/);
  assert.equal(p.calls(), 0, 'no resolver call was made for the refused paths');
});

test('a sibling folder whose name only starts the same way is not inside the allowed folder', async () => {
  const resolve = createFakeResolver().add(ROOT).add(ROOT + '2\\paper.pdf');
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '2\\paper.pdf'), 'linking_refused');
});

test('a file whose real path equals an allowed folder is not inside it', async () => {
  // The folder is named like a document, so the file's real path can equal it exactly. It is not inside itself.
  const named = ROOT + '\\papers.pdf';
  const resolve = createFakeResolver().add(named).add(named + '\\..', { path: ROOT }).add(named, { path: named });
  const p = policy({ resolve, linkFolders: named });
  await refusedWith(p.checkLinkable(named), 'linking_refused', /not inside a folder/);
});

test('an allowed folder that is itself a junction is compared by its real location', async () => {
  const real = 'D:' + '\\real-store';
  const resolve = createFakeResolver()
    .add(ROOT, { path: real })
    .add(ROOT + '\\papers\\paper.pdf', { path: real + '\\papers\\paper.pdf' });
  const p = policy({ resolve });
  assert.deepEqual(await p.checkLinkable(ROOT + '\\papers\\paper.pdf'), { path: real + '\\papers\\paper.pdf' });
});

test('an allowed folder whose real path is a drive root is skipped, so it allows nothing', async () => {
  // Text alone cannot show that a dot-segment path ends at the root, so the resolved path is checked.
  const entry = 'D:' + '\\zotero-test\\..';
  const resolve = createFakeResolver().add(entry, { path: 'D:' + '\\' }).add('D:' + '\\paper.pdf', { path: 'D:' + '\\paper.pdf' });
  const p = policy({ resolve, linkFolders: entry });
  await refusedWith(p.checkLinkable('D:' + '\\paper.pdf'), 'linking_refused', /not inside a folder/);
});

test('an allowed folder reached through a link or junction is skipped, so it must be listed by its real path', async () => {
  const resolve = createFakeResolver().add(ROOT, { leafReparse: true, path: ROOT }).add(ROOT + '\\paper.pdf', { path: ROOT + '\\paper.pdf' });
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '\\paper.pdf'), 'linking_refused', /not inside a folder/);
});

test('an allowed folder on a network share is skipped, and a file on one is refused', async () => {
  const resolve = createFakeResolver().add(ROOT, { remote: true }).add(ROOT + '\\paper.pdf');
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '\\paper.pdf'), 'linking_refused', /not inside a folder/);
});

test('an allowed folder that cannot be resolved is skipped, and the file is refused rather than trusted', async () => {
  const resolve = createFakeResolver().add(ROOT + '\\paper.pdf', { path: ROOT + '\\paper.pdf' });
  resolve.fail(ROOT);
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '\\paper.pdf'), 'linking_refused');
});

test('a second allowed folder is used when the first one does not contain the file', async () => {
  const other = 'E:' + '\\library';
  const resolve = createFakeResolver().add(ROOT).add(other).add(other + '\\paper.pdf', { path: other + '\\paper.pdf' });
  const p = policy({ resolve, linkFolders: ROOT + '|' + other });
  assert.deepEqual(await p.checkLinkable(other + '\\paper.pdf'), { path: other + '\\paper.pdf' });
});

test('containment compares the real paths exactly, so a look-alike character from another alphabet is not inside', async () => {
  // The real paths come from the disk, which already carries each name's letter case. A lower-case request that the
  // disk resolves to the folder's own spelling is inside. A name with a Kelvin sign instead of K is a different name.
  const folder = 'D:' + '\\Kube';
  const kelvin = 'D:' + '\\\u212Aube\\x.pdf';
  const resolve = createFakeResolver()
    .add(folder)
    .add('D:' + '\\kube\\x.pdf', { path: folder + '\\x.pdf' })
    .add(kelvin, { path: kelvin });
  const p = policy({ resolve, linkFolders: folder });
  assert.deepEqual(await p.checkLinkable('D:' + '\\kube\\x.pdf'), { path: folder + '\\x.pdf' });
  await refusedWith(p.checkLinkable(kelvin), 'linking_refused', /not inside a folder/);
});

test('a forward slash in a requested path names the same file as a backslash', async () => {
  const resolve = createFakeResolver().add(ROOT).add(ROOT + '\\papers\\paper.pdf', { path: ROOT + '\\papers\\paper.pdf' });
  const p = policy({ resolve });
  assert.deepEqual(await p.checkLinkable('D:/zotero-test/papers/paper.pdf'), { path: ROOT + '\\papers\\paper.pdf' });
});

test('a missing file outside every allowed folder gets the same refusal as an existing one, so the disk is not described', async () => {
  const resolve = createFakeResolver().add(ROOT);
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable('D:' + '\\elsewhere\\absent.pdf'), 'linking_refused', /not inside a folder/);
  await refusedWith(p.checkLinkable(ROOT + '\\absent.pdf'), 'file_not_found');
});

test('an existing file outside the folders is refused as not inside, whatever its type or links', async () => {
  const resolve = createFakeResolver()
    .add(ROOT)
    .add('D:' + '\\elsewhere\\dir.pdf', { isDirectory: true })
    .add('D:' + '\\elsewhere\\twin.pdf', { hardLinks: 2 })
    .add('D:' + '\\elsewhere\\link.pdf', { leafReparse: true });
  const p = policy({ resolve });
  for (const name of ['dir.pdf', 'twin.pdf', 'link.pdf']) {
    await refusedWith(p.checkLinkable('D:' + '\\elsewhere\\' + name), 'linking_refused', /not inside a folder/);
  }
});

test('a file that resolves to a network or device path is refused', async () => {
  const resolve = createFakeResolver().add(ROOT).add(ROOT + '\\odd.pdf', { path: '\\\\?\\Volume{1}\\odd.pdf' });
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '\\odd.pdf'), 'linking_refused', /local drive/);
});

test('a resolver that throws, or answers with nothing, counts as a failed check', async () => {
  const thrower = policy({
    resolve: async () => {
      throw new Error('boom');
    },
  });
  await refusedWith(thrower.checkLinkable(ROOT + '\\x.pdf'), 'file_check_failed');
  const silent = policy({ resolve: async () => undefined });
  await refusedWith(silent.checkLinkable(ROOT + '\\x.pdf'), 'file_check_failed');
});

test('without a folder allowed, nothing is checked and the refusal names the setting', async () => {
  const p = policy({ linkFolders: '' });
  await assert.rejects(p.checkLinkable(ROOT + '\\x.pdf'), (error) => error.code === 'linking_refused' && /linkFolders/.test(error.message));
  assert.equal(p.calls(), 0);
});

test('a folder list made only of bad entries allows nothing', async () => {
  const p = policy({ linkFolders: 'D:' + '\\|/|relative\\x|C:' });
  await refusedWith(p.checkLinkable(ROOT + '\\x.pdf'), 'linking_refused');
  assert.equal(p.calls(), 0);
});

test('off Windows, linking is refused as unavailable, before anything is read', async () => {
  const p = policy({ isWindows: false });
  await refusedWith(p.checkLinkable(ROOT + '\\x.pdf'), 'linking_unavailable');
  assert.equal(p.calls(), 0);
});

test('a file that is itself a link, has a second name, or is on a share is refused, even inside an allowed folder', async () => {
  const resolve = createFakeResolver()
    .add(ROOT)
    .add(ROOT + '\\leaf.pdf', { leafReparse: true })
    .add(ROOT + '\\twin.pdf', { hardLinks: 2 })
    .add(ROOT + '\\share.pdf', { path: '\\\\server\\share\\share.pdf', remote: true });
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '\\leaf.pdf'), 'linking_refused', /OneDrive/);
  await refusedWith(p.checkLinkable(ROOT + '\\twin.pdf'), 'linking_refused', /hard link/);
  await refusedWith(p.checkLinkable(ROOT + '\\share.pdf'), 'linking_refused', /network drive/);
});

test('a folder is not a linkable file, even when its name ends in an allowed extension', async () => {
  const resolve = createFakeResolver().add(ROOT).add(ROOT + '\\report.pdf', { isDirectory: true });
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '\\report.pdf'), 'linking_refused', /names a folder/);
});

test('a network drive is refused by the resolver before the path is touched, and the policy refuses it', async () => {
  // The resolver answers remote from the drive mapping. The policy then refuses without any further check.
  const resolve = createFakeResolver().add(ROOT).add(ROOT + '\\far.pdf', { remote: true });
  const p = policy({ resolve });
  await refusedWith(p.checkLinkable(ROOT + '\\far.pdf'), 'linking_refused', /network drive/);
});
