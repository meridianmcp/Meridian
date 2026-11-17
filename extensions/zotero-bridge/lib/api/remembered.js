'use strict';

// Remembered apps. When a person chooses "Allow for this app, remembered" at the desktop prompt, the app name goes on
// this list. A later grant request from a remembered name is granted without a prompt (see grants.js).
//
// The list lives in one Zotero preference, as a JSON array of { "name": ..., "approved_at": ... }. The read and write
// functions and the clock are injected, so the node tests run the whole list without Zotero. Text that is not a valid
// list reads as an empty list, and entries that are not valid are dropped. A damaged preference therefore never grants
// anything. It only means the apps prompt again.
//
// Names match without regard to case or surrounding spaces, as the refusal cooldown does. The name is self-reported.
// README, "Write grants", says what that means.

var NAME_MAX = 80;
var MAX_REMEMBERED = 50;

function cleanName(name) {
  return typeof name === 'string' ? name.trim() : '';
}

function normalise(name) {
  return cleanName(name).toLowerCase();
}

// Turns stored text into entries. Malformed text, malformed entries and repeated names are dropped, and the list is
// capped, so a damaged value cannot grow without limit.
function parseList(text) {
  if (typeof text !== 'string' || text.trim() === '') return [];
  var parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  var entries = [];
  var seen = new Set();
  parsed.forEach(function (entry) {
    if (entries.length >= MAX_REMEMBERED) return;
    if (!entry || typeof entry !== 'object') return;
    var name = cleanName(entry.name);
    if (name === '' || name.length > NAME_MAX) return;
    var key = name.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    var approved =
      typeof entry.approved_at === 'string' && !isNaN(Date.parse(entry.approved_at)) ? entry.approved_at : '';
    entries.push({ name: name, approved_at: approved });
  });
  return entries;
}

function createRemembered(deps) {
  if (typeof deps.read !== 'function') throw new Error('createRemembered needs deps.read');
  if (typeof deps.write !== 'function') throw new Error('createRemembered needs deps.write');
  if (typeof deps.now !== 'function') throw new Error('createRemembered needs deps.now');

  // Reads never throw. A preference that cannot be read counts as an empty list here.
  function loadSafe() {
    try {
      return parseList(deps.read());
    } catch (error) {
      return [];
    }
  }

  // Writes read first and let a read error through, so a failed read can never overwrite the stored list.
  function loadStrict() {
    return parseList(deps.read());
  }

  function list() {
    return loadSafe().map(function (entry) {
      return { name: entry.name, approved_at: entry.approved_at };
    });
  }

  function has(name) {
    var key = normalise(name);
    if (key === '') return false;
    return loadSafe().some(function (entry) {
      return entry.name.toLowerCase() === key;
    });
  }

  // Adds a name. Returns true when it was added, and false when it was already there, which keeps its first approval
  // time. Throws when the name is not usable, when the list is full, or when the preference cannot be written.
  function add(name) {
    var clean = cleanName(name);
    if (clean === '' || clean.length > NAME_MAX) {
      throw new Error('An app name to remember must be 1 to ' + NAME_MAX + ' characters.');
    }
    var entries = loadStrict();
    var key = clean.toLowerCase();
    if (
      entries.some(function (entry) {
        return entry.name.toLowerCase() === key;
      })
    ) {
      return false;
    }
    if (entries.length >= MAX_REMEMBERED) {
      throw new Error('The remembered list is full. Revoke an app before remembering another.');
    }
    entries.push({ name: clean, approved_at: new Date(deps.now()).toISOString() });
    deps.write(JSON.stringify(entries));
    return true;
  }

  // Removes every entry that matches the name. Returns how many were removed. Writes only when something changed.
  function remove(name) {
    var key = normalise(name);
    if (key === '') return 0;
    var entries = loadStrict();
    var kept = entries.filter(function (entry) {
      return entry.name.toLowerCase() !== key;
    });
    var removed = entries.length - kept.length;
    if (removed > 0) deps.write(JSON.stringify(kept));
    return removed;
  }

  return {
    list: list,
    has: has,
    add: add,
    remove: remove,
  };
}

module.exports = {
  createRemembered: createRemembered,
  NAME_MAX: NAME_MAX,
  MAX_REMEMBERED: MAX_REMEMBERED,
};
