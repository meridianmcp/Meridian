'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const v = require('../lib/api/validate.js');
const { createCollectionWrites } = require('../lib/api/collections.js');
const { createFakeZotero } = require('./support/fake-zotero.js');

function setup() {
  const zotero = createFakeZotero();
  const tracker = { inBatch: false, touched: [], created: [] };
  const collections = createCollectionWrites({ zotero, validate: v, tracker });
  return { zotero, collections, tracker };
}

function apiErrorMatch(status, code) {
  return (error) => error.isApiError === true && error.httpStatus === status && error.code === code;
}

test('createCollection makes a top-level collection, or a child when parent_key is given', async () => {
  const { zotero, collections } = setup();
  const parent = await collections.createCollection({ name: 'Projects' });
  const child = await collections.createCollection({ name: 'Thesis', parent_key: parent.key });
  const saved = zotero._state.collections.find((c) => c.key === child.key);
  assert.equal(saved.name, 'Thesis');
  assert.equal(saved.parentID, zotero._state.collections.find((c) => c.key === parent.key).id);
  await assert.rejects(collections.createCollection({ name: '' }), apiErrorMatch(422, 'invalid_value'));
});

test('collection replies carry no date_modified, because Zotero 10 keeps none for collections', async () => {
  const { collections } = setup();
  const created = await collections.createCollection({ name: 'Shelf' });
  assert.equal(Object.prototype.hasOwnProperty.call(created, 'date_modified'), false);
  assert.match(created.key, /^[A-Z0-9]{8}$/);
  const updated = await collections.updateCollection(created.key, { name: 'Shelf 2' });
  assert.equal(Object.prototype.hasOwnProperty.call(updated, 'date_modified'), false);
});

test('a collection write that sends expected_modified is refused with 422, and says to use expected_version', async () => {
  const { zotero, collections } = setup();
  const collection = zotero._addCollection('Shelf');
  const item = zotero._addItem('book', { title: 'Book' });
  const refused = (error) =>
    apiErrorMatch(422, 'invalid_value')(error) &&
    /collections have no date_modified/.test(error.message) &&
    /expected_version/.test(error.message) &&
    /synced libraries/.test(error.message);
  const token = { expected_modified: '2026-01-01 00:00:00' };
  await assert.rejects(collections.createCollection({ name: 'New', ...token }), refused);
  await assert.rejects(collections.updateCollection(collection.key, { name: 'Renamed', ...token }), refused);
  await assert.rejects(collections.trashCollection(collection.key, token), refused);
  await assert.rejects(collections.restoreCollection(collection.key, token), refused);
  await assert.rejects(collections.addItems(collection.key, { item_keys: [item.key], ...token }), refused);
  await assert.rejects(collections.removeItems(collection.key, { item_keys: [item.key], ...token }), refused);
  assert.equal(collection.name, 'Shelf');
  assert.equal(collection.deleted, false);
  assert.deepEqual(item.collectionIDs, []);
});

test('expected_version still guards a collection write', async () => {
  const { zotero, collections } = setup();
  const collection = zotero._addCollection('Shelf');
  await assert.rejects(
    collections.updateCollection(collection.key, { expected_version: collection.version + 3, name: 'X' }),
    (error) => error.httpStatus === 409 && error.code === 'version_conflict'
  );
  assert.equal(collection.name, 'Shelf');
  const ok = await collections.updateCollection(collection.key, { expected_version: collection.version, name: 'Y' });
  assert.equal(collection.name, 'Y');
  assert.equal(ok.changed, true);
});

test('updateCollection renames, moves to the top level, and refuses a move under itself or a descendant', async () => {
  const { zotero, collections } = setup();
  const a = zotero._addCollection('A');
  const b = zotero._addCollection('B', a.id);
  const c = zotero._addCollection('C', b.id);
  await assert.rejects(
    collections.updateCollection(a.key, { parent_key: a.key }),
    apiErrorMatch(422, 'invalid_value')
  );
  await assert.rejects(
    collections.updateCollection(a.key, { parent_key: c.key }),
    (error) => error.code === 'invalid_value' && /own ancestor/.test(error.message)
  );
  await collections.updateCollection(b.key, { name: 'Renamed', parent_key: null });
  assert.equal(b.name, 'Renamed');
  assert.equal(b.parentID, false);
  await collections.updateCollection(c.key, { parent_key: a.key });
  assert.equal(c.parentID, a.id);
});

test('trashing and restoring a collection toggles its deleted state and uses the save path', async () => {
  const { zotero, collections } = setup();
  const collection = zotero._addCollection('Old');
  const trashed = await collections.trashCollection(collection.key, {});
  assert.equal(collection.deleted, true);
  assert.equal(trashed.changed, true);
  const again = await collections.trashCollection(collection.key, {});
  assert.equal(again.changed, false);
  await collections.restoreCollection(collection.key, {});
  assert.equal(collection.deleted, false);
});

test('restoring a collection also restores the subcollections that were trashed with it, as Zotero does', async () => {
  const { zotero, collections } = setup();
  const parent = zotero._addCollection('Parent');
  const child = zotero._addCollection('Child', parent.id);
  await collections.trashCollection(parent.key, {});
  assert.equal(child.deleted, true, 'trashing the parent trashes the child');
  const result = await collections.restoreCollection(parent.key, {});
  assert.equal(parent.deleted, false);
  assert.equal(child.deleted, false);
  assert.equal(result.restored_subcollections, 1);
});

test('restoring a collection under a trashed parent is refused until the parent is restored', async () => {
  const { zotero, collections } = setup();
  const parent = zotero._addCollection('Parent');
  const child = zotero._addCollection('Child', parent.id);
  await collections.trashCollection(parent.key, {});
  await assert.rejects(
    collections.restoreCollection(child.key, {}),
    (error) => apiErrorMatch(422, 'invalid_value')(error) && /Restore the parent first/.test(error.message)
  );
  assert.equal(child.deleted, true, 'the refused restore changed nothing');
  await collections.restoreCollection(parent.key, {});
  assert.equal(child.deleted, false);
});

test('the ancestor check walks every level, so a trashed grandparent also refuses the restore', async () => {
  const { zotero, collections } = setup();
  const a = zotero._addCollection('A');
  const b = zotero._addCollection('B', a.id);
  const c = zotero._addCollection('C', b.id);
  await collections.trashCollection(a.key, {});
  // A middle collection that is live under a trashed grandparent.
  b.deleted = false;
  await assert.rejects(collections.restoreCollection(c.key, {}), apiErrorMatch(422, 'invalid_value'));
  assert.equal(c.deleted, true);
});

test('addItems refuses the whole list when one key is unknown, and changes no membership', async () => {
  const { zotero, collections } = setup();
  const target = zotero._addCollection('Target');
  const item = zotero._addItem('book', { title: 'Known' });
  await assert.rejects(
    collections.addItems(target.key, { item_keys: [item.key, 'ZZZZZZZZ'] }),
    apiErrorMatch(404, 'not_found')
  );
  assert.deepEqual(item.collectionIDs, []);
});

test('addItems and removeItems change membership, keep the item, and run in one transaction', async () => {
  const { zotero, collections } = setup();
  const target = zotero._addCollection('Target');
  const one = zotero._addItem('book', { title: 'One' });
  const two = zotero._addItem('book', { title: 'Two' });
  const added = await collections.addItems(target.key, { item_keys: [one.key, two.key] });
  assert.equal(added.item_count, 2);
  assert.deepEqual(one.collectionIDs, [target.id]);
  assert.equal(zotero._calls.transactions, 1);
  await collections.removeItems(target.key, { item_keys: [one.key] });
  assert.deepEqual(one.collectionIDs, []);
  assert.equal(one.deleted, false, 'removing from a collection never trashes the item');
});

test('membership changes save with skipDateModifiedUpdate, as Zotero collection add and remove do', async () => {
  const { zotero, collections } = setup();
  const target = zotero._addCollection('Target');
  const one = zotero._addItem('book', { title: 'One' });
  const two = zotero._addItem('book', { title: 'Two' });
  const seen = [];
  // Both save paths are recorded, because a save outside a batch goes through saveTx().
  const originalSave = zotero.Item.prototype.save;
  zotero.Item.prototype.save = async function (options) {
    seen.push(options);
    return originalSave.call(this, options);
  };
  zotero.Item.prototype.saveTx = async function (options) {
    seen.push(options);
    return originalSave.call(this, options);
  };
  await collections.addItems(target.key, { item_keys: [one.key, two.key] });
  await collections.removeItems(target.key, { item_keys: [one.key] });
  assert.equal(seen.length, 3);
  assert.ok(seen.every((options) => options && options.skipDateModifiedUpdate === true));
});

test('inside a batch a membership change saves on the batch transaction, without nesting another', async () => {
  const { zotero, collections, tracker } = setup();
  const target = zotero._addCollection('Target');
  const item = zotero._addItem('book', { title: 'Inside' });
  tracker.inBatch = true;
  try {
    await zotero.DB.executeTransaction(async () => {
      await collections.addItems(target.key, { item_keys: [item.key] });
    });
  } finally {
    tracker.inBatch = false;
  }
  assert.deepEqual(item.collectionIDs, [target.id]);
  assert.equal(zotero._calls.saveTx, 0);
});

test('inside a batch a restore saves on the batch transaction and opens no transaction of its own', async () => {
  const { zotero, collections, tracker } = setup();
  const parent = zotero._addCollection('Parent');
  const child = zotero._addCollection('Child', parent.id);
  await collections.trashCollection(parent.key, {});
  const transactionsBefore = zotero._calls.transactions;
  tracker.inBatch = true;
  try {
    await zotero.DB.executeTransaction(async () => {
      await collections.restoreCollection(parent.key, {});
    });
  } finally {
    tracker.inBatch = false;
  }
  assert.equal(parent.deleted, false);
  assert.equal(child.deleted, false);
  assert.equal(zotero._calls.transactions - transactionsBefore, 1, 'only the batch transaction ran');
});

test('a membership change that fails part way reloads the items it had already changed', async () => {
  const { zotero, collections } = setup();
  const target = zotero._addCollection('Target');
  const one = zotero._addItem('book', { title: 'One' });
  const two = zotero._addItem('book', { title: 'Two' });
  const reloads = [];
  for (const item of [one, two]) {
    item.reload = async function (dataTypes, reloadUnchanged) {
      reloads.push([this.key, dataTypes, reloadUnchanged]);
    };
  }
  const originalSave = zotero.Item.prototype.save;
  let saves = 0;
  zotero.Item.prototype.save = async function (options) {
    saves += 1;
    if (saves === 2) throw new Error('disk full');
    return originalSave.call(this, options);
  };
  await assert.rejects(collections.addItems(target.key, { item_keys: [one.key, two.key] }), /disk full/);
  assert.deepEqual(reloads, [[one.key, null, true], [two.key, null, true]]);
});

test('a trashed item cannot be added to a collection', async () => {
  const { zotero, collections } = setup();
  const target = zotero._addCollection('Target');
  const item = zotero._addItem('book', { title: 'Gone' });
  item.deleted = true;
  await assert.rejects(collections.addItems(target.key, { item_keys: [item.key] }), apiErrorMatch(422, 'invalid_value'));
});

test('the collection factory refuses to start without the shared tracker', () => {
  const zotero = createFakeZotero();
  assert.throws(() => createCollectionWrites({ zotero, validate: v }), /needs deps\.tracker/);
});
