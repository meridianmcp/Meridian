'use strict';

// A Zotero-shaped fake for the node tests of lib/api/*. It is not Zotero. It keeps
// the two rules that matter for the write API:
//   - saveTx() inside an open transaction throws, because Zotero would wait on itself.
//   - executeTransaction() inside another one throws, for the same reason.
// It records every save path, so a test can see which one a write took.

var TYPES = { journalArticle: 2, book: 7, note: 1, attachment: 14 };
var FIELDS = {
  title: { id: 1, types: [2, 7] },
  DOI: { id: 2, types: [2] },
  date: { id: 3, types: [2, 7] },
  extra: { id: 5, types: [2, 7, 1, 14] },
  publicationTitle: { id: 4, types: [2] },
  bookTitle: { id: 6, types: [7] },
  // url is a base field in Zotero. A fake without it refused a URL as an invalid field, so no test could save one.
  url: { id: 7, types: [2, 7] },
};
var CREATOR_TYPES = { author: 1, editor: 2 };

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

// Zotero stamps dateModified on every save. A counter gives each save a new value here.
var stampCounter = 0;
function stamp() {
  stampCounter += 1;
  return '2026-01-01 00:00:' + String(stampCounter % 60).padStart(2, '0') + ' #' + stampCounter;
}

function createFakeIO() {
  var files = new Map();
  var dirs = new Set();
  return {
    files: files,
    dirs: dirs,
    join: function () {
      return Array.prototype.slice.call(arguments).join('/');
    },
    makeDirectory: async function (path) {
      dirs.add(path);
    },
    write: async function (path, bytes) {
      files.set(path, Uint8Array.from(bytes));
    },
    remove: async function (path, options) {
      var recursive = !!(options && options.recursive);
      Array.from(files.keys()).forEach(function (name) {
        if (name === path || (recursive && name.indexOf(path + '/') === 0)) files.delete(name);
      });
      if (recursive) {
        Array.from(dirs).forEach(function (dir) {
          if (dir === path || dir.indexOf(path + '/') === 0) dirs.delete(dir);
        });
      }
      dirs.delete(path);
    },
  };
}

function createFakeZotero(options) {
  options = options || {};
  var errors = [];
  var calls = { save: 0, saveTx: 0, trash: 0, trashTx: 0, transactions: 0, imports: [], links: [], renames: [] };
  var state = { items: [], collections: [], nextID: 1, nextKey: 1, nextRandom: 1, inTx: false };

  function makeKey() {
    var n = state.nextKey++;
    return ('AAAAAAAA' + n.toString(36).toUpperCase()).slice(-8);
  }

  function snapshot() {
    return {
      items: state.items.slice(),
      data: state.items.map(function (item) {
        return {
          fields: clone(item.fields),
          creators: clone(item.creators),
          tags: clone(item.tags),
          collectionIDs: clone(item.collectionIDs),
          relations: clone(item.relations),
          note: item.note,
          deleted: item.deleted,
          version: item.version,
          dateModified: item.dateModified,
          parentItemID: item.parentItemID,
        };
      }),
      collections: state.collections.slice(),
      collectionData: state.collections.map(function (collection) {
        return {
          name: collection.name,
          parentID: collection.parentID,
          deleted: collection.deleted,
          version: collection.version,
          dateModified: collection.dateModified,
        };
      }),
      nextID: state.nextID,
      nextKey: state.nextKey,
    };
  }

  function restore(snap) {
    state.items = snap.items.slice();
    snap.items.forEach(function (item, index) {
      Object.assign(item, clone(snap.data[index]));
      item.note = snap.data[index].note;
    });
    state.collections = snap.collections.slice();
    snap.collections.forEach(function (collection, index) {
      Object.assign(collection, snap.collectionData[index]);
    });
    state.nextID = snap.nextID;
    state.nextKey = snap.nextKey;
  }

  function FakeItem(type) {
    this.itemType = type;
    this.itemTypeID = TYPES[type];
    this.id = null;
    this.key = null;
    this.version = 0;
    this.dateModified = null;
    this.libraryID = 1;
    this.fields = {};
    this.creators = [];
    this.tags = [];
    this.collectionIDs = [];
    this.relations = [];
    this.deleted = false;
    this.parentItemID = false;
    this.note = null;
  }

  FakeItem.prototype.setField = function (name, value) {
    this.fields[name] = value;
    return true;
  };
  FakeItem.prototype.setCreators = function (list) {
    this.creators = clone(list);
  };
  FakeItem.prototype.setTags = function (list) {
    this.tags = list.map(function (tag) {
      return typeof tag === 'string' ? { tag: tag, type: 0 } : { tag: tag.tag, type: tag.type || 0 };
    });
  };
  FakeItem.prototype.addTag = function (tag, type) {
    if (!this.tags.some(function (existing) { return existing.tag === tag; })) {
      this.tags.push({ tag: tag, type: type || 0 });
    }
  };
  FakeItem.prototype.removeTag = function (tag) {
    this.tags = this.tags.filter(function (existing) { return existing.tag !== tag; });
  };
  FakeItem.prototype.setCollections = function (ids) {
    this.collectionIDs = ids.slice();
  };
  FakeItem.prototype.addToCollection = function (id) {
    if (this.collectionIDs.indexOf(id) < 0) this.collectionIDs.push(id);
  };
  FakeItem.prototype.removeFromCollection = function (id) {
    this.collectionIDs = this.collectionIDs.filter(function (existing) { return existing !== id; });
  };
  FakeItem.prototype.addRelation = function (predicate, object) {
    this.relations.push([predicate, object]);
  };
  FakeItem.prototype.removeRelation = function (predicate, object) {
    this.relations = this.relations.filter(function (r) { return !(r[0] === predicate && r[1] === object); });
  };
  FakeItem.prototype.addRelatedItem = function (other) {
    this.addRelation('dc:relation', 'zotero://select/items/1_' + other.key);
  };
  FakeItem.prototype.removeRelatedItem = async function (other) {
    this.removeRelation('dc:relation', 'zotero://select/items/1_' + other.key);
  };
  FakeItem.prototype.isRegularItem = function () {
    return this.itemType !== 'note' && this.itemType !== 'attachment';
  };
  FakeItem.prototype.isNote = function () {
    return this.itemType === 'note';
  };
  FakeItem.prototype.setNote = function (html) {
    this.note = html;
  };
  FakeItem.prototype.save = async function () {
    calls.save += 1;
    if (!this.id) {
      this.id = state.nextID++;
      this.key = makeKey();
      state.items.push(this);
    }
    this.version += 1;
    this.dateModified = stamp();
    return true;
  };
  FakeItem.prototype.saveTx = async function () {
    calls.saveTx += 1;
    if (state.inTx) throw new Error('saveTx inside a transaction would wait on itself');
    return this.save();
  };

  function FakeCollection() {
    this.id = null;
    this.key = null;
    this.version = 0;
    this.dateModified = null;
    this.libraryID = 1;
    this.name = '';
    this.parentID = false;
    this.deleted = false;
  }
  // Matches Zotero's getDescendents(recursive, type, includeTrashed) for collections.
  FakeCollection.prototype.getDescendents = function (recursive, type, includeTrashed) {
    var out = [];
    var walk = function (parent) {
      state.collections.forEach(function (child) {
        if (child.parentID === parent.id && (includeTrashed || !child.deleted)) {
          out.push({ type: 'collection', id: child.id });
          walk(child);
        }
      });
    };
    walk(this);
    return out;
  };
  FakeCollection.prototype.save = async function () {
    if (!this.id) {
      this.id = state.nextID++;
      this.key = makeKey();
      state.collections.push(this);
    }
    this.version += 1;
    this.dateModified = stamp();
    if (this.deleted) {
      // Zotero trashes descendant collections when a collection is trashed.
      this.getDescendents(false, 'collection', true).forEach(function (d) {
        var child = state.collections.filter(function (c) { return c.id === d.id; })[0];
        if (child) child.deleted = true;
      });
    }
    return true;
  };
  FakeCollection.prototype.saveTx = async function () {
    if (state.inTx) throw new Error('saveTx inside a transaction would wait on itself');
    return this.save();
  };

  function liveItems() {
    return state.items;
  }

  var Items = {
    getByLibraryAndKey: function (libraryID, key) {
      var found = liveItems().filter(function (item) {
        return item.libraryID === libraryID && item.key === key;
      })[0];
      return found || false;
    },
    trash: async function (ids) {
      if (!state.inTx) throw new Error('trash requires a transaction');
      calls.trash += 1;
      ids.forEach(function (id) {
        var item = liveItems().filter(function (candidate) { return candidate.id === id; })[0];
        if (item) {
          item.deleted = true;
          item.version += 1;
          item.dateModified = stamp();
        }
      });
    },
    trashTx: async function (ids) {
      calls.trashTx += 1;
      return DB.executeTransaction(function () {
        return Items.trash(ids);
      });
    },
  };

  var Collections = {
    getByLibraryAndKey: function (libraryID, key) {
      var found = state.collections.filter(function (collection) {
        return collection.libraryID === libraryID && collection.key === key;
      })[0];
      return found || false;
    },
    get: function (id) {
      return state.collections.filter(function (collection) { return collection.id === id; })[0] || false;
    },
  };

  var DB = {
    inTransaction: function () {
      return state.inTx;
    },
    executeTransaction: async function (fn) {
      if (state.inTx) throw new Error('nested executeTransaction would wait on itself');
      calls.transactions += 1;
      var snap = snapshot();
      state.inTx = true;
      try {
        return await fn();
      } catch (error) {
        restore(snap);
        throw error;
      } finally {
        state.inTx = false;
      }
    },
  };

  function attachmentFrom(kind, parentItemID, options) {
    var attachment = new FakeItem('attachment');
    attachment.parentItemID = parentItemID;
    attachment.fields.title = options.title || '';
    attachment.fields.linkMode = kind;
    attachment.fields.contentType = options.contentType || '';
    return attachment;
  }

  var Attachments = {
    importFromFile: async function (opts) {
      calls.imports.push(opts);
      var attachment = attachmentFrom('imported', opts.parentItemID, opts);
      await attachment.save();
      return attachment;
    },
    linkFromFile: async function (opts) {
      calls.links.push(opts);
      var attachment = attachmentFrom('linked', opts.parentItemID, opts);
      await attachment.save();
      return attachment;
    },
  };

  var Tags = {
    rename: async function (libraryID, from, to) {
      calls.renames.push({ libraryID: libraryID, from: from, to: to });
    },
  };

  var randomSequence = 0;
  var zotero = {
    version: '10.0.6',
    Item: FakeItem,
    Collection: FakeCollection,
    Items: Items,
    Collections: Collections,
    Libraries: { userLibraryID: 1 },
    ItemTypes: {
      getID: function (name) {
        return TYPES[name] || false;
      },
    },
    ItemFields: {
      getID: function (name) {
        return FIELDS[name] ? FIELDS[name].id : false;
      },
      isValidForType: function (fieldID, typeID) {
        var entry = Object.keys(FIELDS).map(function (name) { return FIELDS[name]; }).filter(function (field) {
          return field.id === fieldID;
        })[0];
        return !!entry && entry.types.indexOf(typeID) >= 0;
      },
    },
    CreatorTypes: {
      getID: function (name) {
        return CREATOR_TYPES[name] || false;
      },
    },
    Attachments: Attachments,
    Tags: Tags,
    DB: DB,
    Utilities: {
      randomString: function () {
        randomSequence += 1;
        return 'RANDOM' + randomSequence;
      },
    },
    getTempDirectory: function () {
      return { path: options.tempPath || '/tmp/zotero-test' };
    },
    logError: function (error) {
      errors.push(error);
    },
    debug: function () {},
    _state: state,
    _calls: calls,
    _errors: errors,
    _addItem: function (type, fields, extra) {
      var item = new FakeItem(type);
      Object.assign(item.fields, fields || {});
      if (extra && extra.collectionIDs) item.collectionIDs = extra.collectionIDs.slice();
      if (extra && extra.tags) item.setTags(extra.tags);
      item.save();
      return item;
    },
    _addCollection: function (name, parentID) {
      var collection = new FakeCollection();
      collection.name = name;
      collection.parentID = parentID || false;
      collection.save();
      return collection;
    },
  };
  return zotero;
}

module.exports = { createFakeZotero: createFakeZotero, createFakeIO: createFakeIO, TYPES: TYPES };
