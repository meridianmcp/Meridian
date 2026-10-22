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

import { exec } from "node:child_process";

const DEFAULT_BASE_URL = "http://127.0.0.1:23119";
const TAG_PAGE_SIZE = 100;

/** The minimal shape this module needs from a `fetch`-like function: real
 * global `fetch` satisfies this, and so does the tiny hand-built fake
 * response object this file's own test suite injects (`{ok, status,
 * json}`, deliberately not a real `Response` -- see zotero.test.js). */
export interface FetchResponseLike {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}
export type FetchLike = (url: string) => Promise<FetchResponseLike>;

/** A best-effort shell-command runner: resolves `true` if the command ran
 * without error, `false` otherwise -- see `defaultExecImpl` below for why
 * this never rejects. */
export type ExecImpl = (command: string) => Promise<boolean>;

function defaultExecImpl(command: string): Promise<boolean> {
  return new Promise((resolve) => {
    // execImpl deliberately never rejects -- a failure to even ATTEMPT the
    // launch (e.g. `open`/`xdg-open` missing on an unusual Linux setup) must
    // degrade to the existing "Zotero unreachable" result, not throw a new,
    // less clear error out of what was originally just a citation lookup.
    exec(command, (err) => resolve(!err));
  });
}

// --- Auto-start (2026-09-24, Adam's own ask: "auto startup zotero if it's
// off") -------------------------------------------------------------------
//
// Zotero registers itself as the OS handler for the `zotero://` URI scheme
// on install, on every platform -- opening that URI launches the app if it
// isn't already running (same mechanism as clicking a mailto: link), and is
// a no-op (briefly focuses the running app) if it already is. This is
// deliberately used INSTEAD OF hardcoding an install path (Program Files /
// Applications / a Linux package location all vary, and a hardcoded path is
// one more thing to get wrong per-platform) -- the OS's own registered
// handler is the one thing Zotero itself guarantees is correct after a real
// install, on every platform, without this code needing to know or guess
// where the binary actually lives.
const ZOTERO_URI = "zotero://";
const AUTO_START_POLL_INTERVAL_MS = 1000;
const AUTO_START_MAX_WAIT_MS = 10000; // Zotero's own real cold-start time on this machine, confirmed empirically to be a few seconds -- 10s gives real headroom without hanging the caller indefinitely on a genuine failure.

function launchCommandForPlatform(platform: NodeJS.Platform): string {
  if (platform === "win32") return `cmd /c start "" "${ZOTERO_URI}"`;
  if (platform === "darwin") return `open "${ZOTERO_URI}"`;
  return `xdg-open "${ZOTERO_URI}"`; // linux and other POSIX platforms
}

export interface LaunchZoteroOptions {
  execImpl?: ExecImpl;
  platform?: NodeJS.Platform;
}

/**
 * Best-effort launch of the Zotero desktop app via its registered
 * `zotero://` URI handler. Returns `true` if the launch command itself ran
 * without error (NOT a guarantee Zotero actually started -- opening the URI
 * can succeed even if, say, Zotero's own startup then fails for an
 * unrelated reason; the caller's own poll-and-retry is what actually
 * confirms readiness). Never throws.
 */
export async function launchZotero({
  execImpl = defaultExecImpl,
  platform = process.platform,
}: LaunchZoteroOptions = {}): Promise<boolean> {
  try {
    return await execImpl(launchCommandForPlatform(platform));
  } catch {
    return false;
  }
}

/** True for an error that plausibly means "nothing is listening on
 * 127.0.0.1:23119" (Zotero not running), as opposed to "Zotero IS running
 * but returned an error" (fetchAllTags's own explicit non-ok throw, whose
 * message always starts with "Zotero local API returned") -- auto-starting
 * again would not help the second case and would just be a confusing extra
 * side effect on top of a real, different problem. */
function looksLikeZoteroNotRunning(err: unknown): boolean {
  const message = (err instanceof Error && err.message) || String(err);
  return !message.startsWith("Zotero local API returned");
}

export interface FetchAllTagsOptions {
  baseUrl?: string;
  fetchImpl?: FetchLike;
}

/** One page entry from Zotero's `/api/users/0/tags` endpoint -- only the
 * `tag` field this module reads is typed; the real endpoint also returns a
 * `meta.numItems` field this module never uses. */
interface ZoteroTagEntry {
  tag: string;
}

/**
 * Page through every tag in the local Zotero library. Returns a plain
 * array of tag strings. Never throws -- a network failure (Zotero not
 * running) surfaces as a thrown error from the caller's own fetch, which
 * lookupCitationKey below catches and turns into a `resolved: null`
 * (unknown, not "no") result.
 */
export async function fetchAllTags({
  baseUrl = DEFAULT_BASE_URL,
  fetchImpl = fetch,
}: FetchAllTagsOptions = {}): Promise<string[]> {
  const tags: string[] = [];
  let start = 0;
  for (;;) {
    const res = await fetchImpl(`${baseUrl}/api/users/0/tags?start=${start}&limit=${TAG_PAGE_SIZE}`);
    if (!res.ok) throw new Error(`Zotero local API returned ${res.status} fetching tags`);
    const page = await res.json();
    if (!Array.isArray(page) || page.length === 0) break;
    for (const entry of page) tags.push((entry as ZoteroTagEntry).tag);
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
export function findKeyTag(tags: string[], citationKey: string | null | undefined): string | null {
  if (!citationKey) return null;
  const suffix = `:key:${citationKey}`;
  return tags.find((tag) => tag.endsWith(suffix)) ?? null;
}

export interface LookupCitationKeyOptions {
  baseUrl?: string;
  fetchImpl?: FetchLike;
  /** make ONE best-effort attempt to auto-launch Zotero on a fetch failure
   * that looks like "nothing is listening" -- see the function doc below. */
  autoStart?: boolean;
  execImpl?: ExecImpl;
  platform?: NodeJS.Platform;
  autoStartPollIntervalMs?: number;
  autoStartMaxWaitMs?: number;
  sleepImpl?: (ms: number) => Promise<void>;
}

export type LookupCitationKeyResult =
  | { resolved: true; tag: string; title: string | null }
  | { resolved: false }
  | { resolved: null; reason: string };

/** `(err && err.message) || err`, but for an `unknown` catch-clause value:
 * an `Error` instance's own (non-empty) message, falling back to `String(err)`
 * for anything else -- same fallback shape the original untyped code used. */
function describeError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return String(err);
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
 * Auto-start (2026-09-24): pass `autoStart: true` (default `false` -- see
 * below) to make ONE best-effort attempt, on a fetch failure that looks
 * like "nothing is listening on 127.0.0.1:23119" (as opposed to Zotero
 * being up but erroring), to launch Zotero via its registered `zotero://`
 * URI handler (see launchZotero above), poll for the local API to come up
 * for up to `autoStartMaxWaitMs`, and retry the lookup once before giving
 * up. Defaults to `false` (the old fail-fast behavior) rather than `true`
 * DELIBERATELY -- this is a library function used by this file's own test
 * suite and potentially other callers; auto-launching a real GUI
 * application (and blocking for up to autoStartMaxWaitMs) is exactly the
 * kind of side effect that must be opt-in, not silently defaulted on. The
 * real caller that wants this (server.js's citation-validation route) opts
 * in explicitly. Never throws either way.
 */
export async function lookupCitationKey(
  citationKey: string | null | undefined,
  {
    baseUrl = DEFAULT_BASE_URL,
    fetchImpl = fetch,
    autoStart = false,
    execImpl = defaultExecImpl,
    platform = process.platform,
    autoStartPollIntervalMs = AUTO_START_POLL_INTERVAL_MS,
    autoStartMaxWaitMs = AUTO_START_MAX_WAIT_MS,
    sleepImpl = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)),
  }: LookupCitationKeyOptions = {},
): Promise<LookupCitationKeyResult> {
  if (!citationKey) return { resolved: false };
  let tags: string[] | null;
  try {
    tags = await fetchAllTags({ baseUrl, fetchImpl });
  } catch (err) {
    if (!autoStart || !looksLikeZoteroNotRunning(err)) {
      return { resolved: null, reason: `Zotero unreachable: ${describeError(err)}` };
    }

    const launched = await launchZotero({ execImpl, platform });
    if (!launched) {
      return {
        resolved: null,
        reason: `Zotero unreachable and could not be auto-started: ${describeError(err)}`,
      };
    }

    // Bounded by ATTEMPT COUNT, not wall-clock time -- deliberately, so this
    // is fully deterministic under an injected sleepImpl in tests (a real
    // Date.now()-based deadline compared against a mocked, near-instant
    // sleep would loop far more than intended, since fake time and real
    // elapsed time drift apart).
    const maxAttempts = Math.max(1, Math.ceil(autoStartMaxWaitMs / autoStartPollIntervalMs));
    let lastErr: unknown = err;
    tags = null;
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      await sleepImpl(autoStartPollIntervalMs);
      try {
        tags = await fetchAllTags({ baseUrl, fetchImpl });
        lastErr = null;
        break;
      } catch (retryErr) {
        lastErr = retryErr;
      }
    }
    if (tags === null) {
      return {
        resolved: null,
        reason: `Zotero was auto-started but its local API never came up within ${autoStartMaxWaitMs}ms: ${describeError(lastErr)}`,
      };
    }
  }
  const tag = findKeyTag(tags, citationKey);
  if (!tag) return { resolved: false };

  try {
    const res = await fetchImpl(`${baseUrl}/api/users/0/items?tag=${encodeURIComponent(tag)}`);
    if (res.ok) {
      const items = await res.json();
      const title =
        Array.isArray(items) && items[0] && (items[0] as { data?: { title?: string } }).data
          ? (items[0] as { data?: { title?: string } }).data!.title || null
          : null;
      return { resolved: true, tag, title };
    }
  } catch {
    // Fall through -- the tag match itself is the real answer to "does this
    // resolve"; failing to also fetch the item's title is a lesser, non-
    // fatal detail.
  }
  return { resolved: true, tag, title: null };
}
