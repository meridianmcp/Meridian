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

const CONFIG_DIR = join(homedir(), ".meridian-latex");
const CHROME_PROFILE_DIR = join(CONFIG_DIR, "chrome-profile");
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

function ensureConfigDir() {
  mkdirSync(CONFIG_DIR, { recursive: true });
}

/** Builds a `Cookie:` header value from every cookie CDP reports for the
 * given base URL's domain -- not just the known session cookie names,
 * since Overleaf may set auxiliary cookies (CSRF, etc.) a real browser
 * session would also carry, and this client should look as close to "a
 * real logged-in browser tab" as reasonably possible. */
function cookiesToHeader(cookies) {
  return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
}

/**
 * Interactive login flow. MUST be run by a human directly (never invoked
 * from within an agent session) -- opens a real, visible Chrome window.
 *
 * @param {object} [opts]
 * @param {string} [opts.baseUrl]  default "https://www.overleaf.com" -- pass a self-hosted Community Edition URL to log into that instead
 * @returns {Promise<{cookie: string, savedTo: string}>}
 */
export async function login({ baseUrl = DEFAULT_BASE_URL } = {}) {
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
    await waitForLogin(client, baseUrl);

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

/** Polls the page's current URL until it's no longer the login page --
 * the same "wait for navigation away from /login" signal a human watching
 * the window would use themselves. Simple and robust against Overleaf's
 * exact login-success redirect target changing over time (unlike, say,
 * waiting for one specific post-login URL). */
function waitForLogin(client, baseUrl) {
  const { Page } = client;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error("Timed out waiting for login (5 minutes) -- run login() again when ready."));
    }, LOGIN_TIMEOUT_MS);

    const interval = setInterval(async () => {
      try {
        const { frameTree } = await Page.getFrameTree();
        const currentUrl = frameTree.frame.url;
        if (currentUrl && !currentUrl.includes("/login")) {
          cleanup();
          resolve();
        }
      } catch {
        // A transient CDP error mid-poll (e.g. the page is mid-navigation)
        // -- just try again on the next tick rather than failing the whole
        // login over one hiccup.
      }
    }, LOGIN_POLL_INTERVAL_MS);

    function cleanup() {
      clearTimeout(timeout);
      clearInterval(interval);
    }
  });
}

/** Reads the previously-saved cookie, or `null` if `login()` was never
 * run (or `logout()` cleared it). This is the function overleaf-ot-client
 * callers should actually use day-to-day -- login() is a one-time (or
 * once-every-~5-days, per Overleaf's own cookie lifetime) setup step.
 * `cookieFile` is overridable (defaults to the real COOKIE_FILE) purely so
 * this file's own test suite can exercise the real read/parse logic
 * against a throwaway temp file instead of the real `~/.meridian-latex` --
 * every real caller should omit it. */
export function loadSavedCookie(cookieFile = COOKIE_FILE) {
  if (!existsSync(cookieFile)) return null;
  try {
    const data = JSON.parse(readFileSync(cookieFile, "utf-8"));
    return data.cookie || null;
  } catch {
    return null;
  }
}

export function logout(cookieFile = COOKIE_FILE) {
  if (existsSync(cookieFile)) unlinkSync(cookieFile);
}

export function status(cookieFile = COOKIE_FILE) {
  if (!existsSync(cookieFile)) return { loggedIn: false };
  try {
    const data = JSON.parse(readFileSync(cookieFile, "utf-8"));
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
      .catch((err) => {
        console.error(err.message);
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
