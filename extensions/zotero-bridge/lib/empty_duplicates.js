'use strict';

// Duplicate empty sources in one collection. Pure logic, no Zotero globals.
//
// An empty source is an item with no normalised title and no normalised DOI. Earlier imports added the same untitled
// BibTeX entry on every run, which left several empty copies that share one BibTeX key. Rules, for each group of
// candidates that share a key:
//   1. Only items with no title, no DOI, and a usable BibTeX key are candidates. Anything else is left alone.
//   2. The oldest candidate (dateAdded, then id) is kept.
//   3. A later candidate with attachments or notes is kept too and reported as skipped, so no data is lost.
//   4. Every other later candidate is listed for the trash. Nothing is erased here.

const { normalizeTitle, normalizeDoi } = require('./normalize.js');
const { isSafeBibKey } = require('./bibkey.js');

function byOldest(a, b) {
  const added = String(a.dateAdded || '').localeCompare(String(b.dateAdded || ''));
  return added !== 0 ? added : a.id - b.id;
}

function findEmptyDuplicates(items) {
  const groups = new Map();
  for (const item of Array.isArray(items) ? items : []) {
    if (!item || normalizeTitle(item.title) || normalizeDoi(item.doi)) continue;
    const key = typeof item.key === 'string' ? item.key.trim() : '';
    if (!isSafeBibKey(key)) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }

  const trash = [];
  const skipped = [];
  for (const [key, members] of groups) {
    if (members.length < 2) continue;
    members.sort(byOldest);
    for (const item of members.slice(1)) {
      if (item.hasChildren) {
        skipped.push({ id: item.id, key, reason: 'has attachments or notes' });
      } else {
        trash.push({ id: item.id, key });
      }
    }
  }
  return { trash, skipped };
}

module.exports = { findEmptyDuplicates };
