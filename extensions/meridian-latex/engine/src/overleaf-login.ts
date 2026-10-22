// Safe, human-driven Overleaf login for the OT client (overleaf-ot-client.js
// needs a real session cookie; this file is the ONLY place that ever
// touches one). Mirrors the safety pattern netique/overleaf-mcp's own
// README documents for the same problem (a DIFFERENT project, read only
// for this general, standard approach -- not its code): spawn a DEDICATED,
// isolated Chrome profile (never the user's real browser profile), open a
// real interactive window pointed at Overleaf's own login page, let the
// human log in themselves (captcha/OAuth/2FA/SSO all work exactly because
// it's a genuine browser window), then read the resulting session cookie
// back via the Chrome DevTools Protocol -- a real, official, actively-
// maintained Chromium protocol (github.com/ChromeDevTools/devtools-
// protocol, BSD-3-Clause) with lean, permissively-licensed client
// libraries (chrome-launcher, Apache-2.0; chrome-remote-interface, MIT) --
// no reason to hand-roll this one the way socketio09/ had to for the
// legacy, undocumented Socket.IO 0.9.x protocol.
//
// This module NEVER runs inside an agent/AI session -- it exists to be
// invoked directly by a human (`node src/overleaf-login.js` / a future
// `pixi run`-style task), per this project's own hard rule: an agent
// session must never capture, handle, or transmit real account
// credentials or session cookies on a human's behalf.

import { readFileSync, writeFileSync, mkdirSync, existsSync, chmodSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { launch as launchChrome } from "chrome-launcher";
import CDP from "chrome-remote-interface";
import type { CDPCookie } from "chrome-remote-interface";

export const CONFIG_DIR = join(homedir(), ".meridian-latex");
export const CHROME_PROFILE_DIR = join(CONFIG_DIR, "chrome-profile");
const COOKIE_FILE = join(CONFIG_DIR, "cookie.json");

const DEFAULT_BASE_URL = "https://www.overleaf.com";
const LOGIN_POLL_INTERVAL_MS = 1000;
const LOGIN_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes, matching netique's own documented wait window

/** Cookie names Overleaf's own session actually uses. `sharelatex.sid` is
 * the historical/still-current name (ShareLaTeX was Overleaf's earlier
 * name before the 2017 merger; the cookie name was never renamed) --
 * confirmed indirectly via SessionSockets.js's own session-store lookup
 * needing SOME specific cookie name, and this being the long-documented,
 * still-current one in every real client/tool that talks to Overleaf's
 * session layer. `overleaf_session2` is included as a defensive fallback
 * in case Overleaf has since introduced or renamed a session cookie --
 * `login()` below saves EVERY cookie for the domain regardless, so this
 * list only controls what gets flagged as "the" session cookie for a
 * quick sanity check, never limits what's actually persisted. */
const KNOWN_SESSION_COOKIE_NAMES = ["sharelatex.sid", "overleaf_session2"];

export function ensureConfigDir(): void {
  // Real, live bug found 2026-09-24 -- Adam ran login() and got
  // `ENOENT: no such file or directory, open '...\chrome-profile\chrome-out.log'`,
  // and Chrome never opened at all. Root cause: this only created CONFIG_DIR,
  // never CHROME_PROFILE_DIR (the actual userDataDir passed to launchChrome
  // below) -- and chrome-launcher@1.2.1 itself does NOT create userDataDir
  // before use; it opens `${userDataDir}/chrome-out.log` for its own logging
  // BEFORE its one internal mkdirSync call (for a *different* profile dir,
  // confirmed by reading node_modules/chrome-launcher/dist/chrome-launcher.js
  // directly, not assumed from docs). Creating CHROME_PROFILE_DIR here covers
  // CONFIG_DIR too (recursive: true walks all missing parents).
  mkdirSync(CHROME_PROFILE_DIR, { recursive: true });
}

/** Builds a `Cookie:` header value from every cookie CDP reports for the
 * given base URL's domain -- not just the known session cookie names,
 * since Overleaf may set auxiliary cookies (CSRF, etc.) a real browser
 * session would also carry, and this client should look as close to "a
 * real logged-in browser tab" as reasonably possible. */
function cookiesToHeader(cookies: CDPCookie[]): string {
  return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
}

export interface LoginOptions {
  /** default "https://www.overleaf.com" -- pass a self-hosted Community Edition URL to log into that instead */
  baseUrl?: string;
}

export interface LoginResult {
  cookie: string;
  savedTo: string;
}

/**
 * Interactive login flow. MUST be run by a human directly (never invoked
 * from within an agent session) -- opens a real, visible Chrome window.
 */
export async function login({ baseUrl = DEFAULT_BASE_URL }: LoginOptions = {}): Promise<LoginResult> {
  ensureConfigDir();

  console.log(`Opening a dedicated Chrome window for ${baseUrl} -- log in normally.`);
  console.log("This profile is separate from your regular Chrome profile and is only used by meridian-latex.");

  const chrome = await launchChrome({
    userDataDir: CHROME_PROFILE_DIR,
    chromeFlags: ["--no-first-run", "--no-default-browser-check"],
    startingUrl: `${baseUrl}/login`,
  });

  try {
    const client = await CDP({ port: chrome.port });
    const { Network, Page } = client;
    await Network.enable();
    await Page.enable();

    console.log("Waiting for you to finish logging in (up to 5 minutes)...");
    await waitForLogin(client, baseUrl, chrome);

    const { cookies } = await Network.getCookies({ urls: [baseUrl] });
    if (cookies.length === 0) {
      throw new Error(
        "No cookies were found for this domain after login -- the login may not have completed. Try again.",
      );
    }

    const cookieHeader = cookiesToHeader(cookies);
    const hasKnownSessionCookie = cookies.some((c) => KNOWN_SESSION_COOKIE_NAMES.includes(c.name));
    if (!hasKnownSessionCookie) {
      console.warn(
        `Warning: none of the expected session cookie names (${KNOWN_SESSION_COOKIE_NAMES.join(", ")}) were ` +
          "found. Saving what was captured anyway, but the OT client may fail to authenticate -- if it does, " +
          "Overleaf may have renamed its session cookie; check overleaf-ot-client.js's own auth assumptions.",
      );
    }

    writeFileSync(COOKIE_FILE, JSON.stringify({ baseUrl, cookie: cookieHeader, savedAt: new Date().toISOString() }, null, 2));
    try {
      chmodSync(COOKIE_FILE, 0o600);
    } catch {
      // chmod is a no-op on some platforms (notably Windows, which has no
      // real POSIX permission bits) -- best-effort only, never fatal.
    }

    console.log(`Session captured and saved to ${COOKIE_FILE}.`);
    await client.close();
    return { cookie: cookieHeader, savedTo: COOKIE_FILE };
  } finally {
    await chrome.kill();
  }
}

/**
 * THE actual root cause of "the window randomly disappears right when I
 * click Google/ORCID/IEEE" (found 2026-09-24, after Adam's own repro
 * transcript showed a "successful" run whose real effect was killing the
 * window mid-OAuth-flow): the OLD success check was
 * `!currentUrl.includes("/login")` -- true the INSTANT the page navigates
 * to ANY third-party SSO provider's own domain (accounts.google.com/v3/
 * signin/identifier, orcid.org/signin, services*.ieee.org/idp/SSO.saml2 --
 * confirmed via this session's own reproduction captures), since none of
 * those URLs happen to contain the substring "/login" either, despite the
 * human not having entered a single credential yet. waitForLogin then
 * resolved immediately, login() printed "Session captured" (whatever
 * cookies existed for the ORIGINAL baseUrl at that instant, likely stale/
 * pre-auth ones), and its own `finally { chrome.kill() }` closed the window
 * out from under the human mid-flow -- exactly the reported symptom.
 *
 * Correct condition: the browser must be back on OVERLEAF'S OWN hostname
 * (not a third-party IdP mid-handoff) AND not still showing /login. A
 * user's own SSO landing on the identity provider's domain now correctly
 * keeps waiting; the eventual redirect back to Overleaf's OAuth callback
 * (also on Overleaf's hostname, also lacking "/login") correctly resolves,
 * matching the real point at which the human has actually finished
 * authenticating.
 */
export function isBackOnOverleafLoggedIn(currentUrl: string | null | undefined, baseUrl: string): boolean {
  let current: URL;
  let base: URL;
  try {
    // `currentUrl` can be `null`/`undefined` from a caller (see this file's
    // own test suite) -- `?? ""` gives the URL constructor a real string,
    // which still throws for an empty/unparseable input (same "keep
    // waiting" outcome the original untyped code got from `new URL(null)`
    // itself throwing), so this preserves behavior exactly while satisfying
    // URL's `string` parameter type.
    current = new URL(currentUrl ?? "");
    base = new URL(baseUrl);
  } catch {
    return false; // an unparseable URL is never "done" -- keep waiting/polling.
  }
  if (current.hostname !== base.hostname) return false; // still on a third-party IdP (Google/ORCID/IEEE/...)
  return !current.pathname.includes("/login");
}

/** The minimal CDP client shape `waitForLogin` actually needs -- just
 * `Page.getFrameTree()`. Deliberately narrower than the full `CDPClient`
 * type (see the ambient chrome-remote-interface shim) so this file's own
 * test suite can pass a hand-built fake client (no `Network`/`close`) --
 * the real client `login()` passes in (a full `CDPClient`) satisfies this
 * narrower shape structurally either way. */
export interface MinimalCDPClient {
  Page: {
    getFrameTree(): Promise<{ frameTree: { frame: { url: string } } }>;
  };
}

/** The minimal shape of a launched-Chrome handle `waitForLogin` needs --
 * just an optional `process` with the two EventEmitter methods it actually
 * calls. The real `chrome-launcher` `LaunchedChrome.process` (a real
 * `child_process.ChildProcess`) satisfies this structurally; so does this
 * file's own test suite's plain `node:events` `EventEmitter` fake. */
export interface ChromeLike {
  process?: {
    once(event: "exit", listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
    removeListener(event: "exit", listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
  };
}

/** Polls the page's current URL until it's no longer the login page --
 * the same "wait for navigation away from /login" signal a human watching
 * the window would use themselves. Simple and robust against Overleaf's
 * exact login-success redirect target changing over time (unlike, say,
 * waiting for one specific post-login URL).
 *
 * @param chrome  the chrome-launcher instance from login() -- optional so
 *   this function stays independently testable without a real launched
 *   Chrome (see overleaf-login.test.js); when omitted, a crashed/exited
 *   Chrome process is NOT specially detected (falls back to the old
 *   behavior of eventually timing out).
 */
export function waitForLogin(client: MinimalCDPClient, baseUrl: string, chrome?: ChromeLike): Promise<void> {
  const { Page } = client;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error("Timed out waiting for login (5 minutes) -- run login() again when ready."));
    }, LOGIN_TIMEOUT_MS);

    // Safety net for a genuinely unexpected Chrome death (distinct from --
    // and no longer the explanation for -- the real bug this file used to
    // have: see isBackOnOverleafLoggedIn's own comment for the actual root
    // cause of "the window disappears when I click an OAuth provider").
    // Without this, the poll below's own catch{} would treat a truly dead
    // Chrome process identically to a harmless mid-navigation blip and keep
    // silently retrying for the full 5-minute timeout.
    let chromeExited = false;
    const onExit = (code: number | null, signal: NodeJS.Signals | null): void => {
      chromeExited = true;
      cleanup();
      reject(
        new Error(
          `Chrome exited unexpectedly while waiting for login to complete ` +
            `(code=${code}, signal=${signal}) -- this is NOT a normal "timed out" case, ` +
            `Chrome itself closed unprompted. Run login() again.`,
        ),
      );
    };
    chrome?.process?.once("exit", onExit);

    const interval = setInterval(async () => {
      if (chromeExited) return; // onExit already handled this
      try {
        const { frameTree } = await Page.getFrameTree();
        const currentUrl = frameTree.frame.url;
        if (isBackOnOverleafLoggedIn(currentUrl, baseUrl)) {
          cleanup();
          resolve();
        }
      } catch {
        // A transient CDP error mid-poll (e.g. the page is mid-navigation)
        // -- just try again on the next tick rather than failing the whole
        // login over one hiccup. If Chrome has ALSO actually exited, onExit
        // above already rejected this promise; this catch firing too is
        // harmless (a Promise only settles once).
      }
    }, LOGIN_POLL_INTERVAL_MS);

    function cleanup(): void {
      clearTimeout(timeout);
      clearInterval(interval);
      chrome?.process?.removeListener("exit", onExit);
    }
  });
}

/** The on-disk shape `login()` writes to `COOKIE_FILE` and `loadSavedCookie`/
 * `status` read back. */
interface SavedCookieFile {
  baseUrl: string;
  cookie: string;
  savedAt: string;
}

/** Reads the previously-saved cookie, or `null` if `login()` was never
 * run (or `logout()` cleared it). This is the function overleaf-ot-client
 * callers should actually use day-to-day -- login() is a one-time (or
 * once-every-~5-days, per Overleaf's own cookie lifetime) setup step.
 * `cookieFile` is overridable (defaults to the real COOKIE_FILE) purely so
 * this file's own test suite can exercise the real read/parse logic
 * against a throwaway temp file instead of the real `~/.meridian-latex` --
 * every real caller should omit it. */
export function loadSavedCookie(cookieFile: string = COOKIE_FILE): string | null {
  if (!existsSync(cookieFile)) return null;
  try {
    const data = JSON.parse(readFileSync(cookieFile, "utf-8")) as SavedCookieFile;
    return data.cookie || null;
  } catch {
    return null;
  }
}

export function logout(cookieFile: string = COOKIE_FILE): void {
  if (existsSync(cookieFile)) unlinkSync(cookieFile);
}

export type OverleafLoginStatus =
  | { loggedIn: false }
  | { loggedIn: true; baseUrl: string; savedAt: string };

export function status(cookieFile: string = COOKIE_FILE): OverleafLoginStatus {
  if (!existsSync(cookieFile)) return { loggedIn: false };
  try {
    const data = JSON.parse(readFileSync(cookieFile, "utf-8")) as SavedCookieFile;
    return { loggedIn: true, baseUrl: data.baseUrl, savedAt: data.savedAt };
  } catch {
    return { loggedIn: false };
  }
}

// Allow `node src/overleaf-login.js [login|status|logout]` directly.
//
// Real, live bug found 2026-09-19 -- Adam ran this on Windows and got
// silent zero output. Root cause: this used to hand-build the comparison
// string as `file://${argv[1] with backslashes replaced}`, producing
// "file://C:/Users/..." (two slashes before the drive letter) -- but
// Node's real import.meta.url for an absolute Windows path is
// "file:///C:/Users/..." (THREE slashes: the file: scheme's empty host,
// then the path itself starting with "/C:/..."). The two never matched, so
// this guard was permanently false on Windows -- login() (and status/
// logout) were simply never invoked from the CLI, on this platform, ever.
// pathToFileURL() is Node's own documented, cross-platform-correct way to
// build this comparison (this is the recommended ESM replacement for
// CommonJS's `require.main === module`) -- verified live against this
// exact machine before shipping, not assumed from documentation alone.
//
// A second real bug caught immediately after fixing the first, while
// verifying this module still imports cleanly from index.js (the new
// package "main"): pathToFileURL(process.argv[1]) THROWS if argv[1] is
// undefined (e.g. `node -e "import(...)"`, or any context with no real
// script path) -- a real regression versus the old, wrong-but-non-throwing
// `argv[1]?.replace(...)`. Guarded so importing this module (rather than
// running it as the entry point) can never crash regardless of how the
// importing process itself was invoked.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const command = process.argv[2] || "login";
  if (command === "login") {
    login()
      .then(() => process.exit(0))
      .catch((err: unknown) => {
        console.error(err instanceof Error ? err.message : String(err));
        process.exit(1);
      });
  } else if (command === "status") {
    console.log(JSON.stringify(status(), null, 2));
  } else if (command === "logout") {
    logout();
    console.log("Logged out -- saved cookie removed.");
  } else {
    console.error(`Unknown command "${command}". Use: login | status | logout`);
    process.exit(1);
  }
}
