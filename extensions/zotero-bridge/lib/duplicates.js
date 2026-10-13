'use strict';

// Duplicate classification for BibTeX import. Pure logic, no Zotero globals.
//
// Rules, applied in this order for one entry against the existing items:
//   1. A normalised DOI equal to an existing item's normalised DOI -> exact.
//   2. Otherwise, if the normalised title is empty -> new (no title matching).
//   3. A title match is exact only when neither side has a DOI.
//      Any such title match returns exact, even if another title match
//      conflicts.
//   4. Any other title match is a conflict, reported for review and not
//      imported: one side has a DOI and the other does not, or both DOIs are
//      present and differ.
//   5. Anything else -> new.

const { normalizeTitle, normalizeDoi } = require('./normalize.js');

function classify(entry, existing) {
  const entryDoi = normalizeDoi(entry && entry.doi);
  const entryTitle = normalizeTitle(entry && entry.title);
  const items = Array.isArray(existing) ? existing : [];

  if (entryDoi) {
    for (const item of items) {
      if (entryDoi === normalizeDoi(item && item.doi)) return 'exact';
    }
  }

  if (!entryTitle) return 'new';

  let sawConflict = false;
  for (const item of items) {
    if (!item || normalizeTitle(item.title) !== entryTitle) continue;
    if (!entryDoi && !normalizeDoi(item.doi)) return 'exact';
    sawConflict = true;
  }
  return sawConflict ? 'conflict' : 'new';
}

/**
 * Split parsed entries into imports, exact duplicates and conflicts.
 * Entries that will be imported are added to a working copy of the existing
 * list, so a later identical entry in the same file counts as exact.
 * The `existing` argument is not mutated.
 */
function planImport(entries, existing) {
  const known = Array.isArray(existing) ? existing.slice() : [];
  const plan = { toImport: [], exact: [], conflicts: [] };
  for (const entry of Array.isArray(entries) ? entries : []) {
    const verdict = classify(entry, known);
    if (verdict === 'exact') {
      plan.exact.push(entry);
    } else if (verdict === 'conflict') {
      plan.conflicts.push(entry);
    } else {
      plan.toImport.push(entry);
      known.push({ title: entry.title, doi: entry.doi });
    }
  }
  return plan;
}

module.exports = { classify, planImport };
