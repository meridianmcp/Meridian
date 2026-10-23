'use strict';

// Collection writes for the local write API: create, rename, move, trash, restore,
// and item membership. Membership changes never delete an item from the library.
// Same rules as items.js: every check runs before the first mutation, and a save inside a
// batch uses the transaction the batch already holds.
//
// Zotero 10 keeps no date_modified on collections, so collection replies carry none and
// collection writes take expected_version, not expected_modified.

var MAX_DEPTH = 1000;

function createCollectionWrites(deps) {
  var zotero = deps.zotero;
  var v = deps.validate;
  var tracker = deps.tracker;
  var apiError = v.apiError;
  var invalid = v.invalid;

  if (!tracker) throw new Error('createCollectionWrites needs deps.tracker');
  if (!Array.isArray(tracker.created)) tracker.created = [];

  // Set while withTransaction() runs its function inside a transaction it opened itself. It records
  // what that function changes, so a failure can revert it. Saves made there use save(), because
  // saveTx() would open a second transaction inside the first and wait on itself.
  var ownRecord = null;

  function library() {
    return zotero.Libraries.userLibraryID;
  }

  // Records an object a write is about to change, for the batch or for the transaction opened here.
  function touch(object) {
    var record = tracker.inBatch ? tracker : ownRecord;
    if (!record) return;
    if (!object.id) record.created.push(object);
    record.touched.push(object);
  }

  function persist(object, options) {
    touch(object);
    return tracker.inBatch || ownRecord ? object.save(options) : object.saveTx(options);
  }

  // Reloads the cached object from the database. A null data type list reloads every loaded type.
  // reloadUnchanged makes Zotero read them even with no pending change, which a rollback needs.
  async function reloadFromDatabase(object) {
    if (typeof object.reload === 'function') await object.reload(null, true);
  }

  async function persistOrRevert(object, options) {
    try {
      return await persist(object, options);
    } catch (error) {
      try {
        await reloadFromDatabase(object);
      } catch (ignored) {
        // The original error is the one to report.
      }
      throw error;
    }
  }

  // Puts the cache back in step after a failed write. Objects the write created leave Zotero's cache.
  // Objects it changed reload from the database.
  async function revertCache(record) {
    var created = new Set(record.created);
    var seen = new Set();
    for (var i = 0; i < record.touched.length; i++) {
      var object = record.touched[i];
      if (seen.has(object)) continue;
      seen.add(object);
      try {
        if (created.has(object)) {
          var owner = object.ObjectsClass;
          if (owner && typeof owner.unload === 'function') owner.unload(object.id);
        } else {
          await reloadFromDatabase(object);
        }
      } catch (ignored) {
        // One object that cannot reload must not leave the others stale, so the loop goes on.
      }
    }
  }

  // Runs fn in one transaction. Inside a batch the batch already holds one, so fn runs directly.
  // Outside a batch a failure reverts what fn changed in memory, the same as a failed batch does.
  function withTransaction(fn) {
    if (tracker.inBatch) return fn();
    var record = { touched: [], created: [] };
    return zotero.DB.executeTransaction(async function () {
      ownRecord = record;
      try {
        return await fn();
      } finally {
        ownRecord = null;
      }
    }).catch(async function (error) {
      await revertCache(record);
      throw error;
    });
  }

  // Loads a collection even when it is in the trash, so it can be restored.
  function loadCollection(key) {
    var collection = zotero.Collections.getByLibraryAndKey(library(), key);
    if (!collection) {
      throw apiError(404, 'not_found', 'No collection with key ' + key + ' exists in the user library.');
    }
    return collection;
  }

  function loadLiveCollection(key, name) {
    var collection = loadCollection(key);
    if (collection.deleted) throw invalid(name, 'is in the trash');
    return collection;
  }

  // Collections have no date_modified, so the token that works for items cannot be checked here.
  function refuseModifiedToken(body) {
    if (!body || typeof body !== 'object') return;
    if (body.expected_modified !== undefined && body.expected_modified !== null) {
      throw invalid(
        'expected_modified',
        'cannot be used on a collection: collections have no date_modified. Send expected_version instead; it applies to synced libraries'
      );
    }
  }

  function checkVersion(object, body) {
    refuseModifiedToken(body);
    v.checkPrecondition(object, body, 'collection');
  }

  // Moving a collection under one of its own descendants would make a loop. Walk up from the new parent.
  function assertNoCycle(collection, parent) {
    var cursor = parent;
    for (var depth = 0; cursor && depth < MAX_DEPTH; depth++) {
      if (cursor.id === collection.id) {
        throw invalid('parent_key', 'would make the collection its own ancestor');
      }
      cursor = cursor.parentID ? zotero.Collections.get(cursor.parentID) : null;
    }
  }

  // Restoring a collection under a trashed parent would leave it live inside a hidden one, so the parent comes back first.
  function assertAncestorsLive(collection) {
    var cursor = collection.parentID ? zotero.Collections.get(collection.parentID) : null;
    for (var depth = 0; cursor && depth < MAX_DEPTH; depth++) {
      if (cursor.deleted) {
        throw invalid('key', 'is inside a collection that is in the trash. Restore the parent first');
      }
      cursor = cursor.parentID ? zotero.Collections.get(cursor.parentID) : null;
    }
  }

  // Collection replies carry no date_modified, because Zotero 10 does not store one for collections.
  function summary(object, extra) {
    var out = {
      key: object.key,
      version: object.version,
      library_id: object.libraryID,
    };
    if (extra) {
      Object.keys(extra).forEach(function (name) {
        out[name] = extra[name];
      });
    }
    return out;
  }

  async function createCollection(body) {
    v.requireObject(body, 'body');
    refuseModifiedToken(body);
    var name = v.requireString(body.name, 'name', v.LIMITS.name);
    var parent = null;
    if (body.parent_key !== undefined && body.parent_key !== null) {
      parent = loadLiveCollection(v.keyValue(body.parent_key, 'parent_key'), 'parent_key');
    }
    var collection = new zotero.Collection();
    collection.libraryID = library();
    collection.name = name;
    if (parent) collection.parentID = parent.id;
    await persist(collection);
    return summary(collection);
  }

  async function updateCollection(key, body) {
    v.requireObject(body, 'body');
    var collection = loadCollection(key);
    checkVersion(collection, body);
    if (collection.deleted) throw invalid('key', 'is in the trash; restore it before editing');

    var name = body.name === undefined ? undefined : v.requireString(body.name, 'name', v.LIMITS.name);
    var newParentID;
    var moveToTop = false;
    if (body.parent_key !== undefined) {
      if (body.parent_key === null) {
        moveToTop = true;
      } else {
        var parent = loadLiveCollection(v.keyValue(body.parent_key, 'parent_key'), 'parent_key');
        if (parent.id === collection.id) throw invalid('parent_key', 'cannot be the collection itself');
        assertNoCycle(collection, parent);
        newParentID = parent.id;
      }
    }

    // Every check has passed. The mutations below change the cached collection and save it.
    if (name !== undefined) collection.name = name;
    if (moveToTop) {
      collection.parentID = false;
    } else if (newParentID !== undefined) {
      collection.parentID = newParentID;
    }
    var saved = await persistOrRevert(collection);
    return summary(collection, { changed: saved !== false });
  }

  async function setCollectionDeleted(key, body, deleted) {
    var collection = loadCollection(key);
    checkVersion(collection, body);
    if (collection.deleted === deleted) return summary(collection, { deleted: deleted, changed: false });
    collection.deleted = deleted;
    await persistOrRevert(collection);
    return summary(collection, { deleted: deleted, changed: true });
  }

  function trashCollection(key, body) {
    return setCollectionDeleted(key, body, true);
  }

  // Zotero's own restore brings back the subcollections that were trashed with the collection,
  // so this does the same, in one transaction.
  async function restoreCollection(key, body) {
    var collection = loadCollection(key);
    checkVersion(collection, body);
    if (!collection.deleted) return summary(collection, { deleted: false, changed: false });
    assertAncestorsLive(collection);
    return withTransaction(async function () {
      collection.deleted = false;
      await persist(collection);
      var descendants = collection.getDescendents(false, 'collection', true) || [];
      var restored = 0;
      for (var i = 0; i < descendants.length; i++) {
        var child = zotero.Collections.get(descendants[i].id);
        if (child && child.deleted) {
          child.deleted = false;
          await persist(child);
          restored += 1;
        }
      }
      return summary(collection, { deleted: false, changed: true, restored_subcollections: restored });
    });
  }

  function itemKeysFor(body, name) {
    return v.keyList(body.item_keys, name);
  }

  // Resolves every item before the membership changes start, so a bad key changes nothing.
  function resolveItems(keys) {
    return keys.map(function (key, index) {
      var item = zotero.Items.getByLibraryAndKey(library(), key);
      if (!item) {
        throw apiError(404, 'not_found', 'No item with key ' + key + ' exists in the user library.', { field: 'item_keys[' + index + ']' });
      }
      if (item.deleted) throw invalid('item_keys[' + index + ']', 'is in the trash');
      return item;
    });
  }

  // Membership changes skip the date_modified update, as Zotero's own collection add and remove do.
  async function addItems(key, body) {
    v.requireObject(body, 'body');
    refuseModifiedToken(body);
    var collection = loadLiveCollection(key, 'key');
    var items = resolveItems(itemKeysFor(body, 'item_keys'));
    return withTransaction(async function () {
      for (var i = 0; i < items.length; i++) {
        items[i].addToCollection(collection.id);
        await persist(items[i], { skipDateModifiedUpdate: true });
      }
      return summary(collection, { item_count: items.length });
    });
  }

  async function removeItems(key, body) {
    v.requireObject(body, 'body');
    refuseModifiedToken(body);
    var collection = loadCollection(key);
    var items = resolveItems(itemKeysFor(body, 'item_keys'));
    return withTransaction(async function () {
      for (var i = 0; i < items.length; i++) {
        items[i].removeFromCollection(collection.id);
        await persist(items[i], { skipDateModifiedUpdate: true });
      }
      return summary(collection, { item_count: items.length });
    });
  }

  return {
    createCollection: createCollection,
    updateCollection: updateCollection,
    trashCollection: trashCollection,
    restoreCollection: restoreCollection,
    addItems: addItems,
    removeItems: removeItems,
  };
}

module.exports = { createCollectionWrites: createCollectionWrites };
