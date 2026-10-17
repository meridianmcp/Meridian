'use strict';

// Item, note, attachment, relation and tag writes for the local write API.
// `deps.zotero` is Zotero's global in Zotero and a fake in node tests. Every check
// runs before the first mutation, so a rejected request leaves the cached item unchanged.
//
// Two rules keep batches safe. A save inside a batch uses save(), because saveTx() would wait
// for the transaction the batch already holds. Outside a batch it uses saveTx(), which waits for
// any transaction that is already open. Attachment import, tag rename and BibTeX import open their
// own transactions, so /v1/batch does not include them.
//
// deps.tracker is shared with app.js and collections.js. While a batch runs, every object this file
// saves or trashes is recorded there, so a rolled-back batch can bring the cached objects back in line.

function createItemWrites(deps) {
  var zotero = deps.zotero;
  var v = deps.validate;
  var io = deps.io;
  var newId = deps.newId;
  var tempDir = deps.tempDir;
  var tracker = deps.tracker;
  var delay = deps.delay;
  var apiError = v.apiError;
  var invalid = v.invalid;

  if (!tracker) throw new Error('createItemWrites needs deps.tracker');
  if (typeof delay !== 'function') throw new Error('createItemWrites needs deps.delay');
  if (!Array.isArray(tracker.created)) tracker.created = [];

  // A stalled network share must not hold the write queue, so the file check gives up after this long.
  var FILE_CHECK_MS = 5000;
  var TIMED_OUT = {};
  // A body with a long list of bad fields gets a capped reply, with the real count beside it.
  var MAX_LISTED_PROBLEMS = 50;

  function library() {
    return zotero.Libraries.userLibraryID;
  }

  // Records an object that a batch is about to change. An object with no id yet is one the batch creates.
  function touch(object) {
    if (!tracker.inBatch) return;
    if (!object.id) tracker.created.push(object);
    tracker.touched.push(object);
  }

  // Inside a batch the batch's transaction is open, so save() joins it. Outside one, saveTx() opens its own.
  function persist(object, options) {
    touch(object);
    return tracker.inBatch ? object.save(options) : object.saveTx(options);
  }

  // Reloads the cached object from the database. A null data type list reloads every loaded type.
  // reloadUnchanged makes Zotero read them even with no pending change, which a rollback needs.
  async function reloadFromDatabase(object, dataTypes) {
    if (typeof object.reload === 'function') await object.reload(dataTypes, true);
  }

  // Saves, and when the save fails, reloads the cached object so the failed edit does not linger in memory.
  async function persistOrRevert(object) {
    try {
      return await persist(object);
    } catch (error) {
      try {
        await reloadFromDatabase(object, null);
      } catch (ignored) {
        // The original error is the one to report.
      }
      throw error;
    }
  }

  function loadItem(key) {
    var item = zotero.Items.getByLibraryAndKey(library(), key);
    if (!item) throw apiError(404, 'not_found', 'No item with key ' + key + ' exists in the user library.');
    return item;
  }

  function checkVersion(object, body, noun) {
    v.checkPrecondition(object, body, noun || 'item');
  }

  function summary(object, extra) {
    var out = {
      key: object.key,
      version: object.version,
      date_modified: object.dateModified,
      library_id: object.libraryID,
    };
    if (extra) {
      Object.keys(extra).forEach(function (name) {
        out[name] = extra[name];
      });
    }
    return out;
  }

  function planFields(itemTypeID, fields) {
    var entries = [];
    var problems = [];
    if (fields === undefined || fields === null) return { entries: entries, problems: problems };
    v.requireObject(fields, 'fields');
    Object.keys(fields).forEach(function (name) {
      var value = fields[name];
      if (typeof value === 'number' && isFinite(value)) value = String(value);
      if (typeof value !== 'string' || value.length > v.LIMITS.fieldValue) {
        problems.push({ field: name, reason: 'value must be a string of at most ' + v.LIMITS.fieldValue + ' characters' });
        return;
      }
      var fieldID = zotero.ItemFields.getID(name);
      if (!fieldID || !zotero.ItemFields.isValidForType(fieldID, itemTypeID)) {
        problems.push({ field: name, reason: 'is not a field for this item type' });
        return;
      }
      entries.push([name, value]);
    });
    return { entries: entries, problems: problems };
  }

  // Zotero's setCreators() quietly swaps a role the item type does not allow for the primary role.
  // Each role is checked here, before anything is mutated, so the caller hears about it.
  function planCreators(list, itemType, itemTypeID) {
    return v.optionalList(list, 'creators', v.LIMITS.creators).map(function (row, index) {
      var label = 'creators[' + index + ']';
      v.requireObject(row, label);
      var type = v.requireString(row.creatorType === undefined ? 'author' : row.creatorType, label + '.creatorType', 64);
      var typeID = zotero.CreatorTypes.getID(type);
      if (!typeID) throw invalid(label + '.creatorType', 'is not a known creator type');
      if (!zotero.CreatorTypes.isValidForItemType(typeID, itemTypeID)) {
        throw invalid(label + '.creatorType', 'is "' + type + '", which is not a valid creator role for item type ' + itemType);
      }
      if (typeof row.name === 'string' && row.firstName === undefined && row.lastName === undefined) {
        return {
          creatorType: type,
          lastName: v.requireString(row.name, label + '.name', v.LIMITS.name),
          fieldMode: 1,
        };
      }
      return {
        creatorType: type,
        firstName: v.optionalString(row.firstName, label + '.firstName', v.LIMITS.name) || '',
        lastName: v.optionalString(row.lastName, label + '.lastName', v.LIMITS.name) || '',
        fieldMode: 0,
      };
    });
  }

  function tagText(value, label) {
    var text = v.requireString(value, label, v.LIMITS.tag);
    if (text.trim() !== text) throw invalid(label, 'has leading or trailing spaces');
    return text;
  }

  function planTags(list, name) {
    return v.optionalList(list, name, v.LIMITS.tags).map(function (entry, index) {
      var label = name + '[' + index + ']';
      var isObject = entry !== null && typeof entry === 'object' && !Array.isArray(entry);
      var type = isObject && entry.type !== undefined ? entry.type : 0;
      if (type !== 0 && type !== 1) throw invalid(label + '.type', 'must be 0 (manual) or 1 (automatic)');
      return { tag: tagText(isObject ? entry.tag : entry, label), type: type };
    });
  }

  function planTagNames(list, name) {
    return v.optionalList(list, name, v.LIMITS.tags).map(function (entry, index) {
      return tagText(entry, name + '[' + index + ']');
    });
  }

  function planCollections(keys, name) {
    return keys.map(function (key, index) {
      var collection = zotero.Collections.getByLibraryAndKey(library(), key);
      if (!collection) {
        throw apiError(404, 'not_found', 'No collection with key ' + key + ' exists in the user library.', { field: name + '[' + index + ']' });
      }
      if (collection.deleted) throw invalid(name + '[' + index + ']', 'is in the trash');
      return collection.id;
    });
  }

  function planRelation(body, name) {
    v.requireObject(body, name);
    if (body.related_key !== undefined) {
      var other = loadItem(v.keyValue(body.related_key, name + '.related_key'));
      return { kind: 'item', other: other };
    }
    return {
      kind: 'predicate',
      predicate: v.predicateValue(body.predicate, name + '.predicate'),
      object: v.relationObjectValue(body.object, name + '.object'),
    };
  }

  function applyRelation(item, plan) {
    if (plan.kind === 'item') {
      if (plan.other.id === item.id) throw invalid('related_key', 'cannot be the item itself');
      item.addRelatedItem(plan.other);
    } else {
      item.addRelation(plan.predicate, plan.object);
    }
  }

  async function unapplyRelation(item, plan) {
    if (plan.kind === 'item') {
      await item.removeRelatedItem(plan.other);
    } else {
      await item.removeRelation(plan.predicate, plan.object);
    }
  }

  // Lists at most MAX_LISTED_PROBLEMS fields. The total and the truncated flag appear only when the list is cut.
  function throwIfProblems(problems) {
    if (!problems.length) return;
    var details = { invalid_fields: problems.slice(0, MAX_LISTED_PROBLEMS) };
    if (problems.length > MAX_LISTED_PROBLEMS) {
      details.total = problems.length;
      details.truncated = true;
    }
    throw apiError(422, 'invalid_fields', 'Some fields are not valid for this item type.', details);
  }

  async function createItem(body) {
    v.requireObject(body, 'body');
    var itemType = v.requireString(body.item_type, 'item_type', 64);
    if (itemType === 'note' || itemType === 'attachment') {
      throw invalid('item_type', 'must not be ' + itemType + '; use the notes or attachments endpoint');
    }
    var typeID = zotero.ItemTypes.getID(itemType);
    if (!typeID) throw invalid('item_type', 'is not a known Zotero item type');

    var fields = planFields(typeID, body.fields);
    throwIfProblems(fields.problems);
    var plan = {
      creators: body.creators === undefined ? [] : planCreators(body.creators, itemType, typeID),
      tags: planTags(body.tags, 'tags'),
      collections: planCollections(v.keyList(body.collection_keys === undefined ? [] : body.collection_keys, 'collection_keys'), 'collection_keys'),
      relations: v.optionalList(body.relations, 'relations', 100).map(function (row, index) {
        return planRelation(row, 'relations[' + index + ']');
      }),
    };

    var item = new zotero.Item(itemType);
    item.libraryID = library();
    fields.entries.forEach(function (entry) {
      item.setField(entry[0], entry[1]);
    });
    if (plan.creators.length) item.setCreators(plan.creators);
    if (plan.tags.length) item.setTags(plan.tags);
    if (plan.collections.length) item.setCollections(plan.collections);
    plan.relations.forEach(function (relation) {
      applyRelation(item, relation);
    });
    await persist(item);
    return summary(item);
  }

  async function updateItem(key, body) {
    v.requireObject(body, 'body');
    var item = loadItem(key);
    checkVersion(item, body);
    if (body.item_type !== undefined) throw invalid('item_type', 'cannot change on an existing item');

    var fields = planFields(item.itemTypeID, body.fields);
    throwIfProblems(fields.problems);
    var plan = {
      creators: body.creators === undefined ? null : planCreators(body.creators, item.itemType, item.itemTypeID),
      setTags: body.set_tags === undefined ? null : planTags(body.set_tags, 'set_tags'),
      addTags: planTags(body.add_tags, 'add_tags'),
      removeTags: planTagNames(body.remove_tags, 'remove_tags'),
      setCollections: body.set_collections === undefined ? null : planCollections(v.keyList(body.set_collections, 'set_collections'), 'set_collections'),
      addCollections: planCollections(v.keyList(body.add_collections === undefined ? [] : body.add_collections, 'add_collections'), 'add_collections'),
      removeCollections: planCollections(v.keyList(body.remove_collections === undefined ? [] : body.remove_collections, 'remove_collections'), 'remove_collections'),
      addRelations: v.optionalList(body.add_relations, 'add_relations', 100).map(function (row, index) {
        return planRelation(row, 'add_relations[' + index + ']');
      }),
      removeRelations: v.optionalList(body.remove_relations, 'remove_relations', 100).map(function (row, index) {
        return planRelation(row, 'remove_relations[' + index + ']');
      }),
    };

    // Every check has passed. From here on the item changes in memory and is saved.
    fields.entries.forEach(function (entry) {
      item.setField(entry[0], entry[1]);
    });
    if (plan.creators) item.setCreators(plan.creators);
    if (plan.setTags) item.setTags(plan.setTags);
    plan.addTags.forEach(function (tag) {
      item.addTag(tag.tag, tag.type);
    });
    plan.removeTags.forEach(function (name) {
      item.removeTag(name);
    });
    if (plan.setCollections) item.setCollections(plan.setCollections);
    plan.addCollections.forEach(function (id) {
      item.addToCollection(id);
    });
    plan.removeCollections.forEach(function (id) {
      item.removeFromCollection(id);
    });
    plan.addRelations.forEach(function (relation) {
      applyRelation(item, relation);
    });
    for (var i = 0; i < plan.removeRelations.length; i++) {
      await unapplyRelation(item, plan.removeRelations[i]);
    }
    var saved = await persistOrRevert(item);
    return summary(item, { changed: saved !== false });
  }

  async function trashItem(key, body) {
    var item = loadItem(key);
    checkVersion(item, body);
    if (item.deleted) return summary(item, { deleted: true, changed: false });
    touch(item);
    if (tracker.inBatch) {
      await zotero.Items.trash([item.id]);
    } else {
      await zotero.Items.trashTx([item.id]);
    }
    // Items.trash() writes dateModified with SQL and leaves the cached object alone, so reload it before reporting.
    // The trash is already written, so a failed reload must not turn the reply into an error.
    try {
      await reloadFromDatabase(item, ['primaryData']);
    } catch (ignored) {
      // The reply may then show the date from before the trash.
    }
    return summary(item, { deleted: true, changed: true });
  }

  async function restoreItem(key, body) {
    var item = loadItem(key);
    checkVersion(item, body);
    if (!item.deleted) return summary(item, { deleted: false, changed: false });
    item.deleted = false;
    await persistOrRevert(item);
    return summary(item, { deleted: false, changed: true });
  }

  async function createNote(parentKey, body) {
    v.requireObject(body, 'body');
    var parent = loadItem(parentKey);
    if (!parent.isRegularItem()) throw invalid('key', 'must be a regular item to hold a note');
    var html = v.optionalString(body.html, 'html', v.LIMITS.noteHtml) || '';
    var note = new zotero.Item('note');
    note.libraryID = library();
    note.parentItemID = parent.id;
    note.setNote(html);
    await persist(note);
    return summary(note, { parent_key: parent.key });
  }

  async function updateNote(key, body) {
    v.requireObject(body, 'body');
    var note = loadItem(key);
    if (!note.isNote()) throw invalid('key', 'is not a note');
    checkVersion(note, body, 'note');
    // null is refused too. setNote() throws a plain error on it, which would reach the caller as a 500.
    if (body.html === undefined || body.html === null) throw invalid('html', 'is required');
    note.setNote(v.optionalString(body.html, 'html', v.LIMITS.noteHtml));
    var saved = await persistOrRevert(note);
    return summary(note, { changed: saved !== false });
  }

  async function importAttachmentFile(parent, body, title, contentType) {
    var filename = v.safeFilename(v.requireString(body.filename, 'filename', v.LIMITS.filename));
    var bytes = v.decodeBase64(
      v.requireString(body.data_base64, 'data_base64', Math.ceil((v.LIMITS.attachmentBytes * 4) / 3) + 4),
      'data_base64',
      v.LIMITS.attachmentBytes
    );
    // Zotero names the stored copy after the file it imports, so the file keeps its own name
    // inside a fresh folder. The folder is removed whether the import succeeds or not.
    var folder = io.join(tempDir(), 'meridian-api-' + newId());
    var file = io.join(folder, filename);
    await io.makeDirectory(folder, { createAncestors: true, ignoreExisting: true });
    try {
      await io.write(file, bytes);
      return await zotero.Attachments.importFromFile({
        file: file,
        libraryID: library(),
        parentItemID: parent.id,
        title: title,
        contentType: contentType,
      });
    } finally {
      await io.remove(folder, { recursive: true, ignoreAbsent: true });
    }
  }

  // io.stat() can wait a long time on a network share that does not answer. The race gives up after FILE_CHECK_MS.
  // A stat that fails counts as no file, as it did before.
  function statWithin(path) {
    var check = Promise.resolve()
      .then(function () {
        return io.stat(path);
      })
      .catch(function () {
        return null;
      });
    var timeout = Promise.resolve(delay(FILE_CHECK_MS)).then(function () {
      return TIMED_OUT;
    });
    return Promise.race([check, timeout]);
  }

  async function linkAttachmentFile(parent, body, title, contentType) {
    var path = v.requireString(body.path, 'path', v.LIMITS.path);
    if (!v.isAbsolutePath(path)) throw invalid('path', 'must be an absolute path');
    var info = await statWithin(path);
    if (info === TIMED_OUT) {
      throw apiError(
        422,
        'file_check_timed_out',
        'The file could not be checked in time. The drive or network share may be slow or unavailable. Nothing was linked.'
      );
    }
    if (!info || info.type !== 'regular') {
      throw apiError(422, 'file_not_found', 'No file exists at that path.');
    }
    return zotero.Attachments.linkFromFile({
      file: path,
      parentItemID: parent.id,
      title: title,
      contentType: contentType,
    });
  }

  // Not part of /v1/batch: importing a file opens its own transactions.
  async function createAttachment(parentKey, body) {
    v.requireObject(body, 'body');
    var parent = loadItem(parentKey);
    if (!parent.isRegularItem()) throw invalid('key', 'must be a regular item to hold an attachment');
    var mode = v.requireString(body.mode, 'mode', 32);
    var title = v.optionalString(body.title, 'title', v.LIMITS.title);
    var contentType = v.optionalString(body.content_type, 'content_type', v.LIMITS.contentType);
    var attachment;
    if (mode === 'imported_file') {
      attachment = await importAttachmentFile(parent, body, title, contentType);
    } else if (mode === 'linked_file') {
      attachment = await linkAttachmentFile(parent, body, title, contentType);
    } else {
      throw invalid('mode', 'must be imported_file or linked_file');
    }
    if (!attachment || !attachment.key) {
      throw apiError(500, 'attachment_failed', 'Zotero did not create the attachment. Check Zotero’s error console.');
    }
    return summary(attachment, { parent_key: parent.key, mode: mode });
  }

  async function addRelation(key, body) {
    v.requireObject(body, 'body');
    var item = loadItem(key);
    var plan = planRelation(body, 'body');
    applyRelation(item, plan);
    var saved = await persistOrRevert(item);
    return summary(item, { changed: saved !== false });
  }

  async function deleteRelation(key, body) {
    v.requireObject(body, 'body');
    var item = loadItem(key);
    var plan = planRelation(body, 'body');
    await unapplyRelation(item, plan);
    var saved = await persistOrRevert(item);
    return summary(item, { changed: saved !== false });
  }

  // Not part of /v1/batch: Tags.rename opens its own transaction.
  async function renameTag(body) {
    v.requireObject(body, 'body');
    var from = tagText(v.requireString(body.from, 'from', v.LIMITS.tag), 'from');
    var to = tagText(v.requireString(body.to, 'to', v.LIMITS.tag), 'to');
    if (from === to) throw invalid('to', 'must differ from from');
    await requireTagInUserLibrary(from);
    await zotero.Tags.rename(library(), from, to);
    return { from: from, to: to, library_id: library() };
  }

  // Tags.rename() looks the tag up across every library and throws a plain error when it is missing,
  // which would reach the caller as a 500. A tag that exists only in a group library is missing here.
  async function requireTagInUserLibrary(name) {
    var tagID = zotero.Tags.getID(name);
    var itemIDs = tagID ? await zotero.Tags.getTagItems(library(), tagID) : [];
    if (!itemIDs || itemIDs.length === 0) {
      throw apiError(404, 'not_found', 'No item in the user library has the tag "' + name + '".');
    }
  }

  return {
    createItem: createItem,
    updateItem: updateItem,
    trashItem: trashItem,
    restoreItem: restoreItem,
    createNote: createNote,
    updateNote: updateNote,
    createAttachment: createAttachment,
    addRelation: addRelation,
    deleteRelation: deleteRelation,
    renameTag: renameTag,
    loadItem: loadItem,
    checkVersion: checkVersion,
    summary: summary,
    library: library,
  };
}

module.exports = { createItemWrites: createItemWrites };
