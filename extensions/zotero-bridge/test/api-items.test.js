'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const v = require('../lib/api/validate.js');
const { createItemWrites } = require('../lib/api/items.js');
const { createLinkPolicy } = require('../lib/api/link_policy.js');
const { createFakeZotero, createFakeIO } = require('./support/fake-zotero.js');
const { createFakeResolver } = require('./support/fake-resolver.js');

// The one folder these tests allow for linked files. Split literals keep the static check from reading it as machine-local.
const ROOT = 'D:' + '\\zotero-test';

function setup(overrides = {}) {
  const zotero = createFakeZotero();
  const io = createFakeIO();
  const tracker = { inBatch: false, touched: [], created: [] };
  const resolver = createFakeResolver();
  // Every creator role is allowed unless a test narrows it.
  zotero.CreatorTypes.isValidForItemType = () => true;
  const linkPolicy = createLinkPolicy({
    validate: v,
    isWindows: () => overrides.isWindows !== false,
    linkFolders: () => (overrides.linkFolders !== undefined ? overrides.linkFolders : ROOT),
    resolve: overrides.resolve || resolver,
  });
  resolver.add(ROOT);
  const items = createItemWrites({
    zotero,
    validate: v,
    io,
    newId: () => 'ID1',
    tempDir: () => '/tmp/zotero-test',
    tracker,
    linkPolicy: overrides.noPolicy ? null : linkPolicy,
  });
  return { zotero, io, items, tracker, resolver };
}

function apiErrorMatch(status, code) {
  return (error) => error.isApiError === true && error.httpStatus === status && error.code === code;
}

test('createItem refuses an unknown item type and writes nothing', async () => {
  const { zotero, items } = setup();
  await assert.rejects(items.createItem({ item_type: 'notAType' }), apiErrorMatch(422, 'invalid_value'));
  assert.equal(zotero._state.items.length, 0);
});

test('createItem refuses note and attachment types, which have their own endpoints', async () => {
  const { items } = setup();
  await assert.rejects(items.createItem({ item_type: 'note' }), apiErrorMatch(422, 'invalid_value'));
  await assert.rejects(items.createItem({ item_type: 'attachment' }), apiErrorMatch(422, 'invalid_value'));
});

test('createItem reports every invalid field together and saves nothing', async () => {
  const { zotero, items } = setup();
  await assert.rejects(
    items.createItem({ item_type: 'book', fields: { title: 'T', publicationTitle: 'Not for books', nope: 'x' } }),
    (error) => {
      assert.equal(error.code, 'invalid_fields');
      assert.deepEqual(error.details.invalid_fields.map((p) => p.field).sort(), ['nope', 'publicationTitle']);
      assert.equal(error.details.truncated, undefined, 'a short list is not marked as cut');
      return true;
    }
  );
  assert.equal(zotero._state.items.length, 0);
});

test('a body with many unknown fields lists the first 50 and reports the real total', async () => {
  const { zotero, items } = setup();
  const fields = { title: 'T' };
  for (let i = 0; i < 120; i++) fields['nope' + i] = 'x';
  await assert.rejects(items.createItem({ item_type: 'book', fields }), (error) => {
    assert.equal(error.code, 'invalid_fields');
    assert.equal(error.details.invalid_fields.length, 50);
    assert.equal(error.details.total, 120);
    assert.equal(error.details.truncated, true);
    return true;
  });
  assert.equal(zotero._state.items.length, 0);
});

test('createItem saves fields, creators, tags and collections in one save', async () => {
  const { zotero, items } = setup();
  const collection = zotero._addCollection('Reading');
  const result = await items.createItem({
    item_type: 'journalArticle',
    fields: { title: 'A study', DOI: '10.1/x', date: 2024 },
    creators: [{ creatorType: 'author', firstName: 'Ada', lastName: 'Lovelace' }, { name: 'Working Group' }],
    tags: ['alpha', { tag: 'beta', type: 1 }],
    collection_keys: [collection.key],
  });
  const saved = zotero._state.items.find((item) => item.key === result.key);
  assert.equal(saved.fields.DOI, '10.1/x');
  assert.equal(saved.fields.date, '2024');
  assert.deepEqual(saved.creators[0], { creatorType: 'author', firstName: 'Ada', lastName: 'Lovelace', fieldMode: 0 });
  assert.deepEqual(saved.creators[1], { creatorType: 'author', lastName: 'Working Group', fieldMode: 1 });
  assert.deepEqual(saved.tags, [{ tag: 'alpha', type: 0 }, { tag: 'beta', type: 1 }]);
  assert.deepEqual(saved.collectionIDs, [collection.id]);
  assert.equal(zotero._calls.saveTx, 1);
  assert.equal(zotero._calls.transactions, 0);
  assert.equal(result.library_id, 1);
});

test('inside a batch a create saves with save(), so it does not wait on its own transaction', async () => {
  const { zotero, items, tracker } = setup();
  tracker.inBatch = true;
  try {
    await zotero.DB.executeTransaction(async () => {
      await items.createItem({ item_type: 'book', fields: { title: 'Inside a batch' } });
    });
  } finally {
    tracker.inBatch = false;
  }
  assert.equal(zotero._calls.saveTx, 0);
  assert.equal(zotero._state.items.length, 1);
});

test('a creator role the item type does not allow is refused before anything is written, on create and on update', async () => {
  const { zotero, items } = setup();
  // Fake rule: "editor" (id 2) is not a role for journalArticle (id 2). Everything else is allowed.
  zotero.CreatorTypes.isValidForItemType = (creatorTypeID, itemTypeID) => !(creatorTypeID === 2 && itemTypeID === 2);
  await assert.rejects(
    items.createItem({ item_type: 'journalArticle', creators: [{ creatorType: 'author', lastName: 'A' }, { creatorType: 'editor', lastName: 'B' }] }),
    (error) => {
      assert.equal(error.httpStatus, 422);
      assert.equal(error.code, 'invalid_value');
      assert.match(error.message, /creators\[1\]\.creatorType is "editor", which is not a valid creator role for item type journalArticle/);
      return true;
    }
  );
  assert.equal(zotero._state.items.length, 0);
  const item = zotero._addItem('journalArticle', { title: 'Keep' });
  await assert.rejects(
    items.updateItem(item.key, { fields: { title: 'Changed' }, creators: [{ creatorType: 'editor', lastName: 'C' }] }),
    apiErrorMatch(422, 'invalid_value')
  );
  assert.equal(item.fields.title, 'Keep', 'the refused update changed nothing in memory');
  assert.deepEqual(item.creators, []);
});

test('an update with a stale expected_version is refused with 409, and the item is unchanged', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'Old title' });
  await assert.rejects(
    items.updateItem(item.key, { expected_version: item.version + 5, fields: { title: 'New' } }),
    (error) => {
      assert.equal(error.httpStatus, 409);
      assert.equal(error.code, 'version_conflict');
      assert.equal(error.details.current_version, item.version);
      return true;
    }
  );
  assert.equal(item.fields.title, 'Old title');
  assert.equal(zotero._calls.save, 1, 'only the setup save ran');
});

test('an update that names a date_modified it did not read is refused with 409, and an honest one goes through', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'Old title' });
  await assert.rejects(
    items.updateItem(item.key, { expected_modified: '1999-01-01 00:00:00', fields: { title: 'New' } }),
    (error) => error.httpStatus === 409 && error.code === 'changed_since_read' && error.details.current_modified === item.dateModified
  );
  assert.equal(item.fields.title, 'Old title');
  const result = await items.updateItem(item.key, { expected_modified: item.dateModified, fields: { title: 'New' } });
  assert.equal(item.fields.title, 'New');
  assert.equal(result.date_modified, item.dateModified, 'the reply carries the new date_modified for the next write');
});

test('a create reports the key, version and date_modified the caller needs for its next write', async () => {
  const { items } = setup();
  const created = await items.createItem({ item_type: 'book', fields: { title: 'Fresh' } });
  assert.match(created.key, /^[A-Z0-9]{8}$/);
  assert.equal(typeof created.date_modified, 'string');
});

test('an update with a missing collection key changes nothing, not even the tags it was also asked to add', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('journalArticle', { title: 'Keep me' }, { tags: ['keep'] });
  const savesBefore = zotero._calls.save;
  await assert.rejects(
    items.updateItem(item.key, { add_tags: ['new'], set_collections: ['ZZZZZZZZ'] }),
    apiErrorMatch(404, 'not_found')
  );
  assert.deepEqual(item.tags, [{ tag: 'keep', type: 0 }]);
  assert.equal(zotero._calls.save, savesBefore);
});

test('updateItem adds and removes tags and collections, and removes a relation by predicate', async () => {
  const { zotero, items } = setup();
  const first = zotero._addCollection('First');
  const second = zotero._addCollection('Second');
  const item = zotero._addItem('journalArticle', { title: 'T' }, { tags: ['old'], collectionIDs: [second.id] });
  item.addRelation('dc:replaces', 'zotero://select/items/1_OLDONE00');
  await items.updateItem(item.key, {
    add_tags: ['new'],
    remove_tags: ['old'],
    add_collections: [first.key],
    remove_collections: [second.key],
    remove_relations: [{ predicate: 'dc:replaces', object: 'zotero://select/items/1_OLDONE00' }],
  });
  assert.deepEqual(item.tags, [{ tag: 'new', type: 0 }]);
  assert.deepEqual(item.collectionIDs, [first.id]);
  assert.deepEqual(item.relations, []);
});

test('updateItem refuses to change the item type', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'T' });
  await assert.rejects(items.updateItem(item.key, { item_type: 'journalArticle' }), apiErrorMatch(422, 'invalid_value'));
  assert.equal(item.itemType, 'book');
});

test('trash and restore move an item in and out of the trash, using the transaction path', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'T' });
  const trashed = await items.trashItem(item.key, {});
  assert.equal(item.deleted, true);
  assert.equal(trashed.changed, true);
  assert.equal(zotero._calls.trashTx, 1);
  const again = await items.trashItem(item.key, {});
  assert.equal(again.changed, false);
  await items.restoreItem(item.key, {});
  assert.equal(item.deleted, false);
});

test('inside a batch a trash runs trash() in the batch transaction, and does not open a second one', async () => {
  const { zotero, items, tracker } = setup();
  const item = zotero._addItem('book', { title: 'T' });
  const before = { trash: zotero._calls.trash, trashTx: zotero._calls.trashTx, transactions: zotero._calls.transactions };
  tracker.inBatch = true;
  try {
    await zotero.DB.executeTransaction(() => items.trashItem(item.key, {}));
  } finally {
    tracker.inBatch = false;
  }
  assert.equal(item.deleted, true);
  assert.equal(zotero._calls.trash - before.trash, 1);
  assert.equal(zotero._calls.trashTx - before.trashTx, 0);
  assert.equal(zotero._calls.transactions - before.transactions, 1);
});

test('trashItem reports the date_modified the database holds after the trash, not the one from before it', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'T' });
  const reloads = [];
  // Items.trash() writes dateModified with SQL and leaves the cached object alone, so the reload is what changes it here.
  item.reload = async function (dataTypes, reloadUnchanged) {
    reloads.push({ dataTypes, reloadUnchanged });
    this.dateModified = '2030-02-02 10:00:00';
  };
  const out = await items.trashItem(item.key, {});
  assert.deepEqual(reloads, [{ dataTypes: ['primaryData'], reloadUnchanged: true }]);
  assert.equal(out.date_modified, '2030-02-02 10:00:00');
});

test('a reload that fails after the trash is written still answers, because the trash itself succeeded', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'T' });
  item.reload = async () => {
    throw new Error('reload failed');
  };
  const out = await items.trashItem(item.key, {});
  assert.equal(item.deleted, true);
  assert.equal(out.changed, true);
});

test('a failed save reloads the cached item, so the failed edit does not linger in memory', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'Old' });
  const reloads = [];
  item.reload = async (dataTypes, reloadUnchanged) => {
    reloads.push([dataTypes, reloadUnchanged]);
  };
  zotero.Item.prototype.saveTx = async function () {
    throw new Error('database is locked');
  };
  await assert.rejects(items.updateItem(item.key, { fields: { title: 'New' } }), /database is locked/);
  assert.deepEqual(reloads, [[null, true]]);
});

test('outside a batch a save takes saveTx, so an open transaction is not joined by save()', async () => {
  const { zotero, items } = setup();
  // The fake throws where Zotero would wait for the open transaction. Either way the save did not join it.
  zotero._state.inTx = true;
  const saveBefore = zotero._calls.save;
  try {
    await assert.rejects(items.createItem({ item_type: 'book' }), /would wait on itself/);
  } finally {
    zotero._state.inTx = false;
  }
  assert.equal(zotero._calls.saveTx, 1);
  assert.equal(zotero._calls.save, saveBefore);
});

test('an unknown item key is a 404 that names no other item', async () => {
  const { items } = setup();
  await assert.rejects(items.updateItem('ABCDEFGH', { fields: { title: 'x' } }), apiErrorMatch(404, 'not_found'));
});

test('createNote attaches a child note to a regular item and refuses a note as the parent', async () => {
  const { zotero, items } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  const note = await items.createNote(parent.key, { html: '<p>Hello</p>' });
  const child = zotero._state.items.find((item) => item.key === note.key);
  assert.equal(child.itemType, 'note');
  assert.equal(child.parentItemID, parent.id);
  assert.equal(child.note, '<p>Hello</p>');
  assert.equal(note.parent_key, parent.key);
  await assert.rejects(items.createNote(note.key, { html: 'x' }), apiErrorMatch(422, 'invalid_value'));
});

test('updateNote edits only notes, requires html, refuses null html, and reports conflicts', async () => {
  const { zotero, items } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  const note = await items.createNote(parent.key, { html: '<p>One</p>' });
  await assert.rejects(items.updateNote(parent.key, { html: 'x' }), apiErrorMatch(422, 'invalid_value'));
  await assert.rejects(items.updateNote(note.key, {}), apiErrorMatch(422, 'invalid_value'));
  await assert.rejects(
    items.updateNote(note.key, { html: null }),
    (error) => error.httpStatus === 422 && error.code === 'invalid_value' && /html is required/.test(error.message)
  );
  assert.equal(zotero._state.items.find((item) => item.key === note.key).note, '<p>One</p>', 'a refused null changes nothing');
  await assert.rejects(
    items.updateNote(note.key, { expected_version: 99, html: '<p>Two</p>' }),
    apiErrorMatch(409, 'version_conflict')
  );
  const updated = await items.updateNote(note.key, { html: '<p>Two</p>' });
  const saved = zotero._state.items.find((item) => item.key === note.key);
  assert.equal(saved.note, '<p>Two</p>');
  assert.equal(updated.changed, true);
});

test('an imported attachment is written to a fresh folder, imported, and the folder removed', async () => {
  const { zotero, io, items } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  const payload = Buffer.from('%PDF-1.4 hello');
  const attachment = await items.createAttachment(parent.key, {
    mode: 'imported_file',
    filename: 'paper.pdf',
    data_base64: payload.toString('base64'),
    content_type: 'application/pdf',
    title: 'Paper',
  });
  const call = zotero._calls.imports[0];
  assert.equal(call.file, '/tmp/zotero-test/meridian-api-ID1/paper.pdf');
  assert.equal(call.parentItemID, parent.id);
  assert.equal(call.contentType, 'application/pdf');
  assert.equal(io.files.size, 0, 'the temporary copy is removed');
  assert.equal(io.dirs.size, 0, 'the temporary folder is removed');
  assert.equal(attachment.parent_key, parent.key);
  assert.equal(attachment.mode, 'imported_file');
});

test('a bad base64 body or an unsafe file name is refused before any file is written', async () => {
  const { zotero, io, items } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'imported_file', filename: 'a.pdf', data_base64: 'not base64!!' }),
    apiErrorMatch(422, 'invalid_value')
  );
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'imported_file', filename: '../escape.pdf', data_base64: 'AAAA' }),
    apiErrorMatch(422, 'invalid_value')
  );
  assert.equal(io.files.size, 0);
  assert.equal(zotero._calls.imports.length, 0);
});

test('a linked attachment needs an absolute path to a file that exists', async () => {
  const { zotero, items, resolver } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: 'relative\\file.pdf' }),
    apiErrorMatch(422, 'invalid_value')
  );
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\missing.pdf' }),
    apiErrorMatch(422, 'file_not_found')
  );
  resolver.add(ROOT + '\\present.pdf');
  await items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\present.pdf' });
  assert.equal(zotero._calls.links.length, 1);
  assert.equal(zotero._calls.links[0].file, ROOT + '\\present.pdf');
});

test('a linked file is stored at the real path the OS resolves it to, not the path the caller sent', async () => {
  const { zotero, items, resolver } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  resolver.add(ROOT + '\\shortcut\\paper.pdf', { path: ROOT + '\\real\\paper.pdf' });
  await items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\shortcut\\paper.pdf' });
  assert.equal(zotero._calls.links.length, 1);
  assert.equal(zotero._calls.links[0].file, ROOT + '\\real\\paper.pdf');
});

test('a linked file that the OS cannot check is refused with file_check_failed, and nothing is linked', async () => {
  const { zotero, items, resolver } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  resolver.fail(ROOT + '\\broken.pdf');
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\broken.pdf' }),
    apiErrorMatch(422, 'file_check_failed')
  );
  assert.equal(zotero._calls.links.length, 0);
});

test('an attachment needs a regular parent and a known mode', async () => {
  const { zotero, items } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  const note = await items.createNote(parent.key, { html: '' });
  await assert.rejects(
    items.createAttachment(note.key, { mode: 'linked_file', path: '/tmp/zotero-test/x.pdf' }),
    apiErrorMatch(422, 'invalid_value')
  );
  await assert.rejects(items.createAttachment(parent.key, { mode: 'imported_url' }), apiErrorMatch(422, 'invalid_value'));
});

test('addRelation links to another item by key, or stores a predicate, and refuses a self link', async () => {
  const { zotero, items } = setup();
  const a = zotero._addItem('book', { title: 'A' });
  const b = zotero._addItem('book', { title: 'B' });
  await items.addRelation(a.key, { related_key: b.key });
  assert.equal(a.relations.length, 1);
  assert.equal(a.relations[0][0], 'dc:relation');
  await items.addRelation(a.key, { predicate: 'dc:replaces', object: 'zotero://select/items/1_OTHER001' });
  assert.equal(a.relations.length, 2);
  await assert.rejects(items.addRelation(a.key, { related_key: a.key }), apiErrorMatch(422, 'invalid_value'));
  await assert.rejects(items.addRelation(a.key, { predicate: 'not a predicate', object: 'x' }), apiErrorMatch(422, 'invalid_value'));
  await items.deleteRelation(a.key, { related_key: b.key });
  assert.equal(a.relations.length, 1);
});

test('renameTag renames across the user library, and refuses a no-op', async () => {
  const { zotero, items } = setup();
  const lookups = [];
  zotero.Tags.getID = (name) => (name === 'draft' ? 11 : false);
  zotero.Tags.getTagItems = async (libraryID, tagID) => {
    lookups.push([libraryID, tagID]);
    return [5];
  };
  await assert.rejects(items.renameTag({ from: 'same', to: 'same' }), apiErrorMatch(422, 'invalid_value'));
  await items.renameTag({ from: 'draft', to: 'final' });
  assert.deepEqual(lookups, [[1, 11]], 'the tag is looked up in the user library');
  assert.deepEqual(zotero._calls.renames, [{ libraryID: 1, from: 'draft', to: 'final' }]);
});

test('renameTag answers 404 and renames nothing when the tag is not in the user library', async () => {
  const { zotero, items } = setup();
  // "ghost" exists nowhere. "elsewhere" exists, but on no item in the user library, as with a tag held only by a group library.
  zotero.Tags.getID = (name) => (name === 'ghost' ? false : 42);
  zotero.Tags.getTagItems = async () => [];
  await assert.rejects(
    items.renameTag({ from: 'ghost', to: 'x' }),
    (error) => apiErrorMatch(404, 'not_found')(error) && /ghost/.test(error.message)
  );
  await assert.rejects(items.renameTag({ from: 'elsewhere', to: 'x' }), apiErrorMatch(404, 'not_found'));
  assert.deepEqual(zotero._calls.renames, [], 'no rename call was made');
});

test('the write factory refuses to start without the shared tracker', () => {
  const zotero = createFakeZotero();
  const io = createFakeIO();
  assert.throws(
    () => createItemWrites({ zotero, validate: v, io, newId: () => 'x', tempDir: () => '/t' }),
    /needs deps\.tracker/
  );
});

test('a linked file outside every allowed folder is refused, and nothing is linked', async () => {
  const { zotero, items, resolver } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  resolver.add('D:' + '\\elsewhere\\paper.pdf');
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: 'D:' + '\\elsewhere\\paper.pdf' }),
    apiErrorMatch(422, 'linking_refused')
  );
  assert.equal(zotero._calls.links.length, 0);
});

test('a junction inside an allowed folder can neither lead out of it nor into a sensitive folder', async () => {
  const { zotero, items, resolver } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  resolver.add(ROOT + '\\out\\paper.pdf', { path: 'D:' + '\\elsewhere\\paper.pdf' });
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\out\\paper.pdf' }),
    apiErrorMatch(422, 'linking_refused')
  );
  resolver.add(ROOT + '\\keys\\id.pdf', { path: ROOT + '\\.ssh\\id.pdf' });
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\keys\\id.pdf' }),
    apiErrorMatch(422, 'linking_refused')
  );
  assert.equal(zotero._calls.links.length, 0);
});

test('a file that is itself a link, has a second name, sits on a network share, or cannot be checked is refused', async () => {
  const { zotero, items, resolver } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  resolver.add(ROOT + '\\leaf.pdf', { leafReparse: true });
  resolver.add(ROOT + '\\twin.pdf', { hardLinks: 2 });
  resolver.add(ROOT + '\\share.pdf', { path: '\\\\server\\share\\share.pdf', remote: true });
  resolver.fail(ROOT + '\\broken.pdf');
  await assert.rejects(items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\leaf.pdf' }), apiErrorMatch(422, 'linking_refused'));
  await assert.rejects(items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\twin.pdf' }), apiErrorMatch(422, 'linking_refused'));
  await assert.rejects(items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\share.pdf' }), apiErrorMatch(422, 'linking_refused'));
  await assert.rejects(items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\broken.pdf' }), apiErrorMatch(422, 'file_check_failed'));
  assert.equal(zotero._calls.links.length, 0);
});

test('with no folder allowed, nothing can be linked, and the refusal says which setting to change', async () => {
  const { zotero, items, resolver } = setup({ linkFolders: '' });
  const parent = zotero._addItem('book', { title: 'Book' });
  resolver.add(ROOT + '\\present.pdf');
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\present.pdf' }),
    (error) => apiErrorMatch(422, 'linking_refused')(error) && /linkFolders/.test(error.message)
  );
  assert.equal(zotero._calls.links.length, 0);
});

test('the text rules still refuse keys, configs, sensitive folders and device names inside an allowed folder', async () => {
  const { zotero, items, resolver } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  const names = ['\\id_rsa', '\\.env', '\\tool.exe', '\\data.sqlite', '\\.ssh\\notes.pdf', '\\.meridian\\api.txt',
    '\\AppData\\Roaming\\x.pdf', '\\AppData.\\x.pdf', '\\papers\\CON.pdf', '\\notes.txt:hidden'];
  for (const name of names) {
    resolver.add(ROOT + name);
    await assert.rejects(items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + name }), apiErrorMatch(422, 'linking_refused'), name);
  }
  assert.equal(zotero._calls.links.length, 0);
});

test('linking is refused on a platform the resolver does not support', async () => {
  const { zotero, items, resolver } = setup({ isWindows: false });
  const parent = zotero._addItem('book', { title: 'Book' });
  resolver.add(ROOT + '\\present.pdf');
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\present.pdf' }),
    apiErrorMatch(422, 'linking_unavailable')
  );
  assert.equal(zotero._calls.links.length, 0);
});

// Maps each refused key to its reason, so a test can check the key and the place it names.
function reasonsByField(error) {
  return Object.fromEntries(error.details.invalid_fields.map((problem) => [problem.field, problem.reason]));
}

test('createItem refuses top-level keys the body does not read, names where each belongs, and saves nothing', async () => {
  const { zotero, items } = setup();
  const collection = zotero._addCollection('Reading');
  await assert.rejects(
    items.createItem({
      item_type: 'journalArticle',
      title: 'Dropped before',
      DOI: '10.1/x',
      date: '2024',
      url: 'https://example.org/paper',
      collections: [collection.key],
    }),
    (error) => {
      assert.equal(error.httpStatus, 422);
      assert.equal(error.code, 'invalid_fields');
      const reasons = reasonsByField(error);
      assert.deepEqual(Object.keys(reasons).sort(), ['DOI', 'collections', 'date', 'title', 'url']);
      assert.match(reasons.title, /fields\.title/);
      assert.match(reasons.DOI, /fields\.DOI/);
      assert.match(reasons.url, /fields\.url/);
      assert.match(reasons.collections, /collection_keys/);
      return true;
    }
  );
  assert.equal(zotero._state.items.length, 0, 'nothing was saved, not even the parts that were valid');
});

test('an unknown top-level key is refused even when it sits beside valid ones', async () => {
  const { zotero, items } = setup();
  await assert.rejects(
    items.createItem({ item_type: 'book', fields: { title: 'Valid' }, extra_thing: 'x' }),
    (error) => {
      assert.equal(error.code, 'invalid_fields');
      assert.deepEqual(Object.keys(reasonsByField(error)), ['extra_thing']);
      assert.match(reasonsByField(error).extra_thing, /not a key of this request/);
      return true;
    }
  );
  assert.equal(zotero._state.items.length, 0);
});

test('updateItem refuses a top-level title or tags, which it never read, and changes nothing', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'Keep' }, { tags: ['keep'] });
  const savesBefore = zotero._calls.save;
  await assert.rejects(items.updateItem(item.key, { title: 'Dropped before', tags: ['new'] }), (error) => {
    assert.equal(error.httpStatus, 422);
    assert.equal(error.code, 'invalid_fields');
    const reasons = reasonsByField(error);
    assert.match(reasons.title, /fields\.title/);
    assert.match(reasons.tags, /add_tags/);
    return true;
  });
  assert.equal(item.fields.title, 'Keep');
  assert.deepEqual(item.tags, [{ tag: 'keep', type: 0 }]);
  assert.equal(zotero._calls.save, savesBefore);
});

test('updateItem still accepts every key it reads, including the precondition fields', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'Old' });
  await items.updateItem(item.key, {
    expected_version: item.version,
    fields: { title: 'New' },
    add_tags: ['fresh'],
    remove_tags: [],
  });
  assert.equal(item.fields.title, 'New');
  assert.deepEqual(item.tags, [{ tag: 'fresh', type: 0 }]);
});

test('a creator with a snake_case key is refused by name, and nothing is saved as an empty creator', async () => {
  const { zotero, items } = setup();
  await assert.rejects(
    items.createItem({ item_type: 'book', fields: { title: 'T' }, creators: [{ creatorType: 'author', first_name: 'Ada', last_name: 'Lovelace' }] }),
    (error) => {
      assert.equal(error.httpStatus, 422);
      assert.equal(error.code, 'invalid_fields');
      assert.deepEqual(Object.keys(reasonsByField(error)).sort(), ['creators[0].first_name', 'creators[0].last_name']);
      assert.match(reasonsByField(error)['creators[0].first_name'], /is not a creator key/);
      return true;
    }
  );
  await assert.rejects(
    items.createItem({ item_type: 'book', fields: { title: 'T' }, creators: [{ creator_type: 'author', lastName: 'Lovelace' }] }),
    (error) => {
      assert.equal(error.code, 'invalid_fields');
      assert.deepEqual(Object.keys(reasonsByField(error)), ['creators[0].creator_type']);
      return true;
    }
  );
  assert.equal(zotero._state.items.length, 0);
});

test('every unknown creator key is named, across all creators, and nothing is saved', async () => {
  const { zotero, items } = setup();
  await assert.rejects(
    items.createItem({
      item_type: 'book',
      fields: { title: 'T' },
      creators: [
        { creatorType: 'author', firstName: 'Ada', lastName: 'Lovelace' },
        { creatorType: 'editor', first_name: 'Grace', lastName: 'Hopper' },
        { name: 'Working Group', institution: 'Somewhere' },
      ],
    }),
    (error) => {
      assert.equal(error.code, 'invalid_fields');
      assert.deepEqual(Object.keys(reasonsByField(error)).sort(), ['creators[1].first_name', 'creators[2].institution']);
      return true;
    }
  );
  assert.equal(zotero._state.items.length, 0);
});

test('valid creators with each allowed key still save in the same shape', async () => {
  const { zotero, items } = setup();
  const result = await items.createItem({
    item_type: 'book',
    fields: { title: 'T' },
    creators: [
      { creatorType: 'author', firstName: 'Ada', lastName: 'Lovelace' },
      { creatorType: 'editor', name: 'Working Group' },
      { lastName: 'Hopper' },
    ],
  });
  const saved = zotero._state.items.find((item) => item.key === result.key);
  assert.deepEqual(saved.creators[0], { creatorType: 'author', firstName: 'Ada', lastName: 'Lovelace', fieldMode: 0 });
  assert.deepEqual(saved.creators[1], { creatorType: 'editor', lastName: 'Working Group', fieldMode: 1 });
  assert.deepEqual(saved.creators[2], { creatorType: 'author', firstName: '', lastName: 'Hopper', fieldMode: 0 });
});

test('a creator with snake_case keys is refused on update too, and the item is unchanged', async () => {
  const { zotero, items } = setup();
  const item = zotero._addItem('book', { title: 'Keep' });
  await assert.rejects(
    items.updateItem(item.key, { fields: { title: 'Changed' }, creators: [{ first_name: 'Ada', last_name: 'L' }] }),
    (error) => {
      assert.equal(error.httpStatus, 422);
      assert.equal(error.code, 'invalid_fields');
      assert.deepEqual(Object.keys(reasonsByField(error)).sort(), ['creators[0].first_name', 'creators[0].last_name']);
      return true;
    }
  );
  assert.equal(item.fields.title, 'Keep');
  assert.deepEqual(item.creators, []);
});

test('a creator gives either name or firstName and lastName, and at least one name', async () => {
  const { zotero, items } = setup();
  await assert.rejects(
    items.createItem({ item_type: 'book', fields: { title: 'T' }, creators: [{ name: 'Working Group', lastName: 'Ignored' }] }),
    (error) => error.code === 'invalid_value' && /has name together with firstName or lastName/.test(error.message)
  );
  await assert.rejects(
    items.createItem({ item_type: 'book', fields: { title: 'T' }, creators: [{ creatorType: 'author' }] }),
    (error) => error.code === 'invalid_value' && /creators\[0\] needs a name/.test(error.message)
  );
  await assert.rejects(
    items.createItem({ item_type: 'book', fields: { title: 'T' }, creators: [{ name: 42 }] }),
    (error) => error.code === 'invalid_value' && /creators\[0\]\.name must be a string/.test(error.message)
  );
  assert.equal(zotero._state.items.length, 0);
});

test('createItem saves a url field inside fields, with the title beside it', async () => {
  const { zotero, items } = setup();
  const result = await items.createItem({
    item_type: 'journalArticle',
    fields: { title: 'Linked', url: 'https://example.org/paper' },
  });
  const saved = zotero._state.items.find((item) => item.key === result.key);
  assert.equal(saved.fields.title, 'Linked');
  assert.equal(saved.fields.url, 'https://example.org/paper');
});

test('a 4.5 MB imported attachment decodes in full and reaches the importer, where a 3.2 MB one did before', async () => {
  const { zotero, io, items } = setup();
  const parent = zotero._addItem('book', { title: 'Book' });
  const written = [];
  const write = io.write;
  io.write = async (path, bytes) => {
    written.push(bytes.length);
    return write(path, bytes);
  };
  const payload = Buffer.alloc(4500000, 0x41);
  payload.write('%PDF-1.4', 0, 'latin1');
  const attachment = await items.createAttachment(parent.key, {
    mode: 'imported_file',
    filename: 'big.pdf',
    data_base64: payload.toString('base64'),
  });
  assert.deepEqual(written, [4500000]);
  assert.equal(zotero._calls.imports.length, 1);
  assert.equal(attachment.mode, 'imported_file');
});

test('a write factory built without a link policy refuses linking, and fails closed', async () => {
  const { zotero, items, resolver } = setup({ noPolicy: true });
  const parent = zotero._addItem('book', { title: 'Book' });
  resolver.add(ROOT + '\\present.pdf');
  await assert.rejects(
    items.createAttachment(parent.key, { mode: 'linked_file', path: ROOT + '\\present.pdf' }),
    apiErrorMatch(422, 'linking_unavailable')
  );
  assert.equal(zotero._calls.links.length, 0);
});