'use strict';

// Reads the "BibTeX key: <key>" line that the plugin uses to link a Zotero
// item to a <key>.pdf file. Pure logic, no Zotero globals.

// The line must hold exactly one token after the label.
const BIBKEY_LINE = /^[ \t]*BibTeX key:[ \t]*(\S+)[ \t\r]*$/im;

// Keys are used to build a file name, so they must not carry path separators,
// leading dots, or whitespace. ".." is refused too.
const SAFE_KEY = /^[A-Za-z0-9_:+\-][A-Za-z0-9_.:+\-]*$/;
const MAX_KEY_LENGTH = 200;

function isSafeBibKey(key) {
  return (
    typeof key === 'string' &&
    key.length > 0 &&
    key.length <= MAX_KEY_LENGTH &&
    SAFE_KEY.test(key) &&
    !key.includes('..')
  );
}

/**
 * Return the BibTeX key from an Extra field, or null when there is no key line
 * or the key is not safe to use in a file name.
 */
function readBibKey(extra) {
  if (typeof extra !== 'string') return null;
  const match = extra.match(BIBKEY_LINE);
  if (!match) return null;
  return isSafeBibKey(match[1]) ? match[1] : null;
}

// A "Citation Key: <key>" line in Extra, as Zotero's own exports write it.
const CITATION_KEY_LINE = /^[ \t]*Citation Key:[ \t]*(\S+)[ \t\r]*$/im;

/**
 * The citation key of an entry that the BibTeX import translator produced, or null.
 *
 * The translator keeps the entry's key in `itemID` and does not write it to Extra on
 * import, so `itemID` is the source that matters. A `citationKey` field or a
 * `Citation Key:` line in Extra is used first when present. Each candidate must pass
 * the same safety rules as a key read from the plugin's own line.
 */
function citationKeyFromImport(json) {
  if (!json || typeof json !== 'object') return null;
  const candidates = [json.citationKey];
  if (typeof json.extra === 'string') {
    const match = json.extra.match(CITATION_KEY_LINE);
    if (match) candidates.push(match[1]);
  }
  candidates.push(json.itemID);
  for (const candidate of candidates) {
    if (isSafeBibKey(candidate)) return candidate;
  }
  return null;
}

module.exports = { readBibKey, isSafeBibKey, citationKeyFromImport };
