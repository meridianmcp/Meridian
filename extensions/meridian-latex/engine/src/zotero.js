// Citation-key validation against the user's local Zotero library --
// item 6160d667 piece 2. Confirmed live (2026-09-18) rather than assumed:
// this machine's Zotero desktop app exposes a local, unauthenticated HTTP
// API on 127.0.0.1:23119 (Zotero's own "local API", distinct from the
// remote zotero.org web API -- `/api/users/0/...` is Zotero's own documented
// shorthand for "the current local user", so no real numeric Zotero user id
// ever needs to be discovered or hardcoded here). The Meridian-hosted
// `zotero-mcp` tunnel slot was confirmed DISABLED (get_tunnel_diagnostics)
// and Better BibTeX was confirmed NOT installed (its /better-bibtex/
// json-rpc endpoint 404s) -- so there is no citation-key field to query
// directly. What IS real and already in active use in this Zotero library:
// a manual tagging convention, `<project-prefix>:key:<citekey>` (e.g.
// "P1:key:margulies2005454"), applied to items that back the dnabert
// paper's bibliography. This module looks for a tag ending in
// `:key:<citekey>` regardless of the project-prefix, since a citekey should
// only ever collide with one tag suffix in practice and we don't want to
// hardcode "P1" (a prefix specific to one paper) into general-purpose code.
//
// Zotero's own item-search-by-tag endpoint only supports an EXACT tag match
// (confirmed empirically: `?tag=P1:key:foo` finds the item, but a leading
// `*` wildcard does not, and `?search=`/`?q=` on the tags endpoint are
// silently ignored) -- so an unknown prefix can't be looked up server-side
// in one call. This library's total tag count (219, confirmed live) is
// small enough to page through in full and filter client-side instead.

const DEFAULT_BASE_URL = "http://127.0.0.1:23119";
const TAG_PAGE_SIZE = 100;

/**
 * Page through every tag in the local Zotero library. Returns a plain
 * array of tag strings. Never throws -- a network failure (Zotero not
 * running) surfaces as a thrown error from the caller's own fetch, which
 * lookupCitationKey below catches and turns into a `resolved: null`
 * (unknown, not "no") result.
 */
export async function fetchAllTags({ baseUrl = DEFAULT_BASE_URL, fetchImpl = fetch } = {}) {
  const tags = [];
  let start = 0;
  for (;;) {
    const res = await fetchImpl(`${baseUrl}/api/users/0/tags?start=${start}&limit=${TAG_PAGE_SIZE}`);
    if (!res.ok) throw new Error(`Zotero local API returned ${res.status} fetching tags`);
    const page = await res.json();
    if (!Array.isArray(page) || page.length === 0) break;
    for (const entry of page) tags.push(entry.tag);
    if (page.length < TAG_PAGE_SIZE) break;
    start += TAG_PAGE_SIZE;
  }
  return tags;
}

/**
 * Pure (no network) lookup: does any tag in `tags` end in exactly
 * `:key:<citationKey>`? Returns the matching tag string, or `null`.
 * Exact suffix match, not a substring -- "foo:key:smith2020" must not match
 * a search for "smith20" (a truncated/misspelled key is exactly the case
 * this feature exists to catch, not paper over).
 */
export function findKeyTag(tags, citationKey) {
  if (!citationKey) return null;
  const suffix = `:key:${citationKey}`;
  return tags.find((tag) => tag.endsWith(suffix)) || null;
}

/**
 * Full lookup: does `citationKey` resolve to a real item in the user's
 * local Zotero library, via the `:key:` tag convention? Three-way result,
 * deliberately not a boolean -- "Zotero isn't running" is a categorically
 * different situation from "this key doesn't exist", and a caller (the
 * popup UI) should say so distinctly rather than implying a citation key is
 * wrong when the real issue is that nothing could be checked at all:
 *
 *   {resolved: true, tag, title}   -- found; `title` is the matched item's
 *                                     own title, best-effort (a failure
 *                                     fetching item details still reports
 *                                     resolved:true with title:null, since
 *                                     the KEY question -- does this resolve
 *                                     at all -- was already answered).
 *   {resolved: false}              -- Zotero reachable, no matching tag.
 *   {resolved: null, reason}       -- couldn't check (Zotero not running,
 *                                     network error, etc.) -- `reason` is a
 *                                     human-readable explanation.
 *
 * Never throws.
 */
export async function lookupCitationKey(citationKey, { baseUrl = DEFAULT_BASE_URL, fetchImpl = fetch } = {}) {
  if (!citationKey) return { resolved: false };
  let tags;
  try {
    tags = await fetchAllTags({ baseUrl, fetchImpl });
  } catch (err) {
    return { resolved: null, reason: `Zotero unreachable: ${(err && err.message) || err}` };
  }
  const tag = findKeyTag(tags, citationKey);
  if (!tag) return { resolved: false };

  try {
    const res = await fetchImpl(`${baseUrl}/api/users/0/items?tag=${encodeURIComponent(tag)}`);
    if (res.ok) {
      const items = await res.json();
      const title = Array.isArray(items) && items[0] && items[0].data ? items[0].data.title || null : null;
      return { resolved: true, tag, title };
    }
  } catch {
    // Fall through -- the tag match itself is the real answer to "does this
    // resolve"; failing to also fetch the item's title is a lesser, non-
    // fatal detail.
  }
  return { resolved: true, tag, title: null };
}
