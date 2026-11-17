'use strict';

// Pick the collection a menu command should act on, by exact name.
// Pure logic, no Zotero globals.

/**
 * @param {Array<{id: number, name: string}>} collections  candidates from one library
 * @param {string} name  exact, case-sensitive name to match
 * @param {number} [preferredId]  id of the collection the menu was opened on,
 *   used only to break a tie between several collections with the same name
 * @returns {{status: 'ok', collection, matches} | {status: 'none', matches} | {status: 'ambiguous', matches}}
 */
function pickCollectionByName(collections, name, preferredId) {
  const list = Array.isArray(collections) ? collections : [];
  if (typeof name !== 'string' || name === '') {
    return { status: 'none', matches: [] };
  }
  const matches = list.filter((c) => c && c.name === name);
  if (matches.length === 0) return { status: 'none', matches };
  if (matches.length === 1) return { status: 'ok', collection: matches[0], matches };
  const preferred = matches.find((c) => c.id === preferredId);
  if (preferred) return { status: 'ok', collection: preferred, matches };
  return { status: 'ambiguous', matches };
}

module.exports = { pickCollectionByName };
