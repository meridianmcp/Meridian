'use strict';

// Pure normalisation helpers for duplicate detection.
// No Zotero globals and no Node-only APIs, so this file loads in Zotero's
// sandbox and in node --test alike.

/**
 * Normalised title used for duplicate matching:
 * BibTeX braces removed, lowercased, whitespace runs collapsed to one space,
 * and leading/trailing punctuation and whitespace trimmed. Internal
 * punctuation is kept, so "Foo: a bar" and "Foo a bar" stay different.
 */
function normalizeTitle(title) {
  if (typeof title !== 'string') return '';
  return title
    .replace(/[{}]/g, '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .replace(/^[\s\p{P}\p{S}]+|[\s\p{P}\p{S}]+$/gu, '')
    .trim();
}

/**
 * Normalised DOI: braces and whitespace removed, lowercased, and any
 * https/http doi.org or dx.doi.org prefix or a "doi:" scheme stripped.
 * Returns an empty string when there is no usable DOI.
 */
function normalizeDoi(doi) {
  if (typeof doi !== 'string') return '';
  let value = doi.replace(/[{}]/g, '').trim().toLowerCase();
  value = value.replace(/^https?:\/\/(?:www\.)?(?:dx\.)?doi\.org\//, '');
  value = value.replace(/^doi:\s*/, '');
  return value.trim();
}

module.exports = { normalizeTitle, normalizeDoi };
