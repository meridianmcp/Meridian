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

// The PDF step turns a key into a file name, and two things go wrong there on Windows. A colon names an alternate
// data stream, so doe:2019.pdf addresses a stream, not a file. A device stem such as CON, AUX or COM1 opens the
// device, whatever the extension. The import keeps such keys as data, so this stricter check is for file names only.
const DEVICE_STEM = /^(CON|CONIN\$|CONOUT\$|CLOCK\$|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])$/i;

function isSafeFileKey(key) {
  return isSafeBibKey(key) && key.indexOf(':') === -1 && !DEVICE_STEM.test(key.split('.')[0]);
}

/**
 * The token after the BibTeX key label, as written, or null when the Extra field has no key line. It is not
 * checked. A caller that builds a file name from it must use isSafeFileKey first.
 */
function readBibKeyToken(extra) {
  if (typeof extra !== 'string') return null;
  const match = extra.match(BIBKEY_LINE);
  return match ? match[1] : null;
}

/**
 * Return the BibTeX key from an Extra field, or null when there is no key line or the key fails isSafeBibKey,
 * which is the import's rule. That does not make the key safe for a file name. Use isSafeFileKey for that.
 */
function readBibKey(extra) {
  const token = readBibKeyToken(extra);
  return token !== null && isSafeBibKey(token) ? token : null;
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

module.exports = { readBibKey, readBibKeyToken, isSafeBibKey, isSafeFileKey, citationKeyFromImport };
