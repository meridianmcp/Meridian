// Auto-launch a real Overleaf browser window with the meridian-latex
// extension pre-loaded -- item 2026-09-24, Adam's ask: an alternative to
// finishing/relying on the OT client (overleaf-ot-client.js, still blocked
// on a human running overleaf-login.js's login() at least once). Rather than
// a human manually opening Chrome, navigating to Overleaf, and loading the
// extension by hand every time, the Node engine server can do all three
// itself via chrome-launcher (already a dependency, already used the same
// way in overleaf-login.js for the one-time credential-capture flow).
//
// DELIBERATELY NOT headless, and this must stay that way: Adam's own
// explicit requirement is that a human can still see and directly interact
// with this window -- click around, log in manually if the saved session
// expired, watch an edit happen -- not just a background robot process.
// chrome-launcher's launch() opens a real, visible window by default; never
// add a `--headless` flag here.
//
// Reuses overleaf-login.js's CHROME_PROFILE_DIR (not a separate profile) so
// a session captured via `node src/overleaf-login.js login` carries over --
// this window opens already logged in, rather than asking a human to log in
// a second time in a second, unrelated profile.

import { launch as launchChrome } from "chrome-launcher";
import type { Options as ChromeLaunchOptions } from "chrome-launcher";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ensureConfigDir, CHROME_PROFILE_DIR, type ChromeLike } from "./overleaf-login.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEFAULT_EXTENSION_PATH = resolve(__dirname, "..", "..", "extension");
const DEFAULT_PROJECT_URL = "https://www.overleaf.com/project";

/** True for chrome-launcher's own ChromeNotInstalledError, WITHOUT
 * importing the class directly -- the installed chrome-launcher version
 * (confirmed: 1.2.1) exports it from its internal utils.js but not from the
 * package's own public entry point (`Object.keys(require('chrome-launcher'))`
 * is just `Launcher, getChromePath, killAll, launch` -- checked directly,
 * not assumed), so `instanceof` against an unexported class isn't reliably
 * available here. Matching on the constructor name is what the class's own
 * fixed, documented message ("No Chrome installations found.") maps to --
 * confirmed by reading node_modules/chrome-launcher/dist/utils.js directly. */
function isChromeNotInstalledError(err: unknown): boolean {
  const asRecord = err as { constructor?: { name?: string }; message?: string } | null | undefined;
  return Boolean(err) && (asRecord?.constructor?.name === "ChromeNotInstalledError" || /no chrome installations found/i.test(asRecord?.message || ""));
}

/** `(err && err.message) || err`, but for an `unknown` catch-clause value:
 * an `Error` instance's own (non-empty) message, falling back to `String(err)`
 * for anything else -- same fallback shape the original untyped code used. */
function describeError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return String(err);
}

/** The minimal shape `launchOverleafBrowser` needs from its `launchImpl`'s
 * resolved value. Deliberately NOT the full chrome-launcher `LaunchedChrome`
 * type: this function never reads a field off `chrome` itself -- it's a
 * pass-through returned to the CALLER, who is the one that actually calls
 * `chrome.kill()`/reads `chrome.process` (see server.js's own usage), per
 * this function's own JSDoc contract below. Reuses overleaf-login.ts's own
 * `ChromeLike` shape for `process` (rather than redefining it), so both the
 * real chrome-launcher `LaunchedChrome` value AND this file's own hand-built
 * test fakes (which vary `kill()`'s sync/async-ness and sometimes omit
 * `process` altogether) satisfy it without a cast. Deliberately NO index
 * signature here (unlike range-locate.ts's own `OutlineNodeLike`): an index
 * signature would force every comparison to require the SOURCE to also
 * declare one, which chrome-launcher's own named `LaunchedChrome` interface
 * does not -- only `kill` and the inherited `process` are actually needed. */
export interface LaunchedChromeLike extends ChromeLike {
  kill?: () => unknown;
}

export interface LaunchOverleafBrowserOptions {
  /** a full Overleaf project URL, or the plain project-list URL by default */
  projectUrl?: string;
  /** absolute path to the unpacked extension directory */
  extensionPath?: string;
  /** Chrome user-data-dir; defaults to the SAME profile overleaf-login.js's
   * login() uses, so a saved session carries over */
  profileDir?: string;
  /** injectable for tests -- defaults to chrome-launcher's real launch() */
  launchImpl?: (opts: ChromeLaunchOptions) => Promise<LaunchedChromeLike>;
  ensureConfigDirImpl?: () => void;
}

export type LaunchOverleafBrowserResult =
  | { chrome: LaunchedChromeLike; error: null }
  | { chrome: null; error: string };

/**
 * Launch a real, visible Chrome window with the meridian-latex extension
 * loaded, pointed at an Overleaf project. Never throws -- returns
 * `{chrome, error: null}` on success (caller should keep `chrome` around
 * and call `chrome.kill()` when done with it, same contract as
 * chrome-launcher's own launch()) or `{chrome: null, error}` with a clear,
 * actionable message on failure.
 */
export async function launchOverleafBrowser({
  projectUrl = DEFAULT_PROJECT_URL,
  extensionPath = DEFAULT_EXTENSION_PATH,
  profileDir = CHROME_PROFILE_DIR,
  launchImpl = launchChrome,
  ensureConfigDirImpl = ensureConfigDir,
}: LaunchOverleafBrowserOptions = {}): Promise<LaunchOverleafBrowserResult> {
  try {
    // Same real bug class fixed in overleaf-login.js today: chrome-launcher
    // opens a log file inside userDataDir before it creates that directory
    // itself. Must be created here too -- this function does not assume
    // login() has ever been run first.
    ensureConfigDirImpl();

    const chrome = await launchImpl({
      userDataDir: profileDir,
      chromeFlags: [
        "--no-first-run",
        "--no-default-browser-check",
        `--load-extension=${extensionPath}`,
        `--disable-extensions-except=${extensionPath}`,
      ],
      startingUrl: projectUrl,
    });
    return { chrome, error: null };
  } catch (err) {
    if (isChromeNotInstalledError(err)) {
      return {
        chrome: null,
        error:
          "Chrome is not installed (or could not be found) on this machine. Install Google Chrome, " +
          "or point CHROME_PATH at a Chromium-based browser's executable, then try again.",
      };
    }
    return { chrome: null, error: `Failed to launch the Overleaf automation browser: ${describeError(err)}` };
  }
}
