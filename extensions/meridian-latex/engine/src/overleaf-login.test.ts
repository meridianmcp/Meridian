import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import { EventEmitter } from "node:events";
import {
  loadSavedCookie,
  status,
  logout,
  ensureConfigDir,
  CONFIG_DIR,
  CHROME_PROFILE_DIR,
  waitForLogin,
  isBackOnOverleafLoggedIn,
} from "./overleaf-login.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

// loadSavedCookie()/status()/logout() all take an optional cookieFile path
// (defaulting to the real ~/.meridian-latex/cookie.json) precisely so this
// suite can exercise the real read/parse/delete logic against a throwaway
// temp file, never the real one. (login() itself -- the one function that
// spawns a real, credential-entering browser window -- is explicitly NOT
// unit-tested here or anywhere: it must only ever be run interactively by
// a human. See overleaf-login.js's own header comment.)

function withTempDir<T>(fn: (dir: string) => T): T {
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-login-test-"));
  try {
    return fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/** The shape `execFileSync` actually throws when the spawned process exits
 * non-zero (Node has no dedicated exported type for this -- it's a plain
 * `Error` decorated with these extra fields at runtime; see child_process's
 * own `ExecFileException`, which covers the callback-style shape but not
 * quite this sync-throw one). Scoped to just the two fields this test file
 * reads. */
interface ExecFileSyncError {
  status: number | null;
  stderr: string;
}

test("loadSavedCookie(): a well-formed saved file round-trips", () => {
  withTempDir((dir) => {
    const cookieFile = join(dir, "cookie.json");
    writeFileSync(
      cookieFile,
      JSON.stringify({ baseUrl: "https://www.overleaf.com", cookie: "sharelatex.sid=abc123", savedAt: "2026-09-18T00:00:00.000Z" }),
    );
    assert.equal(loadSavedCookie(cookieFile), "sharelatex.sid=abc123");
  });
});

test("loadSavedCookie(): a missing file returns null, never throws", () => {
  withTempDir((dir) => {
    assert.equal(loadSavedCookie(join(dir, "does-not-exist.json")), null);
  });
});

test("loadSavedCookie(): a corrupted (non-JSON) file returns null, never throws", () => {
  withTempDir((dir) => {
    const cookieFile = join(dir, "cookie.json");
    writeFileSync(cookieFile, "{ not valid json");
    assert.equal(loadSavedCookie(cookieFile), null);
  });
});

test("status(): reports loggedIn true with baseUrl/savedAt for a real saved file", () => {
  withTempDir((dir) => {
    const cookieFile = join(dir, "cookie.json");
    writeFileSync(
      cookieFile,
      JSON.stringify({ baseUrl: "https://www.overleaf.com", cookie: "x", savedAt: "2026-09-18T00:00:00.000Z" }),
    );
    const s = status(cookieFile);
    assert.equal(s.loggedIn, true);
    assert.equal((s as { baseUrl: string }).baseUrl, "https://www.overleaf.com");
    assert.equal((s as { savedAt: string }).savedAt, "2026-09-18T00:00:00.000Z");
  });
});

test("status(): reports loggedIn false when no file exists", () => {
  withTempDir((dir) => {
    assert.deepEqual(status(join(dir, "does-not-exist.json")), { loggedIn: false });
  });
});

test("logout(): removes the saved cookie file", () => {
  withTempDir((dir) => {
    const cookieFile = join(dir, "cookie.json");
    writeFileSync(cookieFile, JSON.stringify({ baseUrl: "x", cookie: "y" }));
    assert.equal(existsSync(cookieFile), true);
    logout(cookieFile);
    assert.equal(existsSync(cookieFile), false);
  });
});

test("logout(): a no-op (never throws) when there's nothing to remove", () => {
  withTempDir((dir) => {
    assert.doesNotThrow(() => logout(join(dir, "does-not-exist.json")));
  });
});

// Real, live bug found 2026-09-24: Adam ran `node src/overleaf-login.js
// login` and got `ENOENT: ...open '...\chrome-profile\chrome-out.log'` --
// Chrome never opened at all. ensureConfigDir() used to only create
// CONFIG_DIR, never CHROME_PROFILE_DIR (the actual userDataDir passed to
// chrome-launcher's launch()) -- and chrome-launcher@1.2.1 itself opens
// `${userDataDir}/chrome-out.log` for logging BEFORE its own one internal
// mkdirSync call (confirmed by reading node_modules/chrome-launcher/dist/
// chrome-launcher.js directly). This exercises the REAL exported
// ensureConfigDir() against the real ~/.meridian-latex paths (there is
// nothing to parameterize here -- chrome-launcher itself is hardwired to
// CHROME_PROFILE_DIR) -- safe because mkdirSync(..., {recursive:true}) is
// a pure, idempotent no-op if the directories already exist.
test("ensureConfigDir(): creates CHROME_PROFILE_DIR (not just CONFIG_DIR) -- chrome-launcher needs it to exist before launch()", () => {
  ensureConfigDir();
  assert.equal(existsSync(CONFIG_DIR), true, "CONFIG_DIR must exist");
  assert.equal(existsSync(CHROME_PROFILE_DIR), true, "CHROME_PROFILE_DIR must exist -- this is the actual userDataDir chrome-launcher writes chrome-out.log into before creating anything itself");
});

// --- CLI entry-point guard: must actually run as a real subprocess -------
//
// Real, live bug found 2026-09-19 (Adam ran `node src/overleaf-login.js
// login` on his real Windows machine and got silent zero output): the
// bottom-of-file `if (import.meta.url === ...)` guard used to hand-build
// its comparison string, which never matched import.meta.url's REAL format
// on Windows (three slashes -- "file:///C:/...", the scheme's empty host
// plus the path -- vs. the guard's own two-slash "file://C:/..."). This
// meant `login()`/`status()`/`logout()` were NEVER invoked from the CLI on
// Windows, silently. Fixed via node:url's pathToFileURL(), the documented,
// cross-platform-correct ESM replacement for CommonJS's
// `require.main === module`.
//
// This class of bug is invisible to a normal `import { status } from
// "./overleaf-login.js"` test (as used everywhere else in this file) --
// import.meta.url in that case is the TEST FILE's own url, and
// process.argv[1] is the test runner's entry point, so the guard correctly
// stays false regardless of whether the comparison logic itself is broken.
// The only real way to exercise this exact bug is to actually spawn this
// file as its own process, exactly how a human running it from a terminal
// does -- which is what these tests do.

test("CLI: `node overleaf-login.js status` actually runs (the exact command that was silently broken)", () => {
  const scriptPath = join(__dirname, "overleaf-login.js");
  const output = execFileSync(process.execPath, [scriptPath, "status"], { encoding: "utf-8" });
  const parsed = JSON.parse(output);
  assert.equal(typeof parsed.loggedIn, "boolean", "status must actually run and print real JSON, not silently produce nothing");
});

test("CLI: an unrecognized subcommand actually runs and exits non-zero with a clear message", () => {
  const scriptPath = join(__dirname, "overleaf-login.js");
  assert.throws(
    () => execFileSync(process.execPath, [scriptPath, "not-a-real-command"], { encoding: "utf-8", stdio: "pipe" }),
    (err: unknown) => {
      const e = err as ExecFileSyncError;
      assert.equal(e.status, 1);
      assert.match(e.stderr, /Unknown command/);
      return true;
    },
  );
});

// Real regression found 2026-09-19, caught immediately after fixing the
// Windows guard above, while verifying this module still imports cleanly
// as part of index.js (the package's new npm "main"): pathToFileURL(argv[1])
// THROWS if argv[1] is undefined -- a real regression versus the old,
// wrong-but-non-throwing `argv[1]?.replace(...)`. `node -e` is exactly a
// context where argv[1] is undefined; this test reproduces that directly
// rather than relying on index.js's own happy path to catch it.
// --- isBackOnOverleafLoggedIn (real, live bug, 2026-09-24) -----------------
//
// THE actual root cause of "clicking Google/ORCID/IEEE makes the window
// disappear", confirmed from Adam's own repro transcript: the old check was
// `!currentUrl.includes("/login")`, which is true the INSTANT the page
// navigates to a third-party SSO provider's own domain (none of those
// happen to contain "/login" either), long before the human has entered a
// single credential. waitForLogin resolved immediately, login() printed a
// false "Session captured", and its own chrome.kill() closed the window
// out from under the human mid-OAuth-flow. These are the real URLs
// captured live during this session's own reproduction attempts.

const BASE_URL = "https://www.overleaf.com";

test("isBackOnOverleafLoggedIn: the actual live-captured Google identifier URL is NOT yet logged in", () => {
  assert.equal(
    isBackOnOverleafLoggedIn(
      "https://accounts.google.com/v3/signin/identifier?opparams=%253F&client_id=47304055603.apps.googleusercontent.com",
      BASE_URL,
    ),
    false,
  );
});

test("isBackOnOverleafLoggedIn: the actual live-captured ORCID URL is NOT yet logged in", () => {
  assert.equal(isBackOnOverleafLoggedIn("https://orcid.org/signin?response_type=code&client_id=APP-7LF990G7W35DXRZT", BASE_URL), false);
});

test("isBackOnOverleafLoggedIn: the actual live-captured IEEE SAML URL is NOT yet logged in", () => {
  assert.equal(isBackOnOverleafLoggedIn("https://services10.ieee.org/idp/SSO.saml2?SAMLRequest=abc123", BASE_URL), false);
});

test("isBackOnOverleafLoggedIn: still on Overleaf's own /login page is NOT logged in", () => {
  assert.equal(isBackOnOverleafLoggedIn("https://www.overleaf.com/login", BASE_URL), false);
});

test("isBackOnOverleafLoggedIn: back on Overleaf at a normal post-login URL IS logged in", () => {
  assert.equal(isBackOnOverleafLoggedIn("https://www.overleaf.com/project", BASE_URL), true);
});

test("isBackOnOverleafLoggedIn: Overleaf's own OAuth callback URL (same hostname, no /login) IS logged in", () => {
  // The real redirect_uri captured live during this session's Google repro.
  assert.equal(isBackOnOverleafLoggedIn("https://www.overleaf.com/users/auth/google_oauth2/callback?code=abc", BASE_URL), true);
});

test("isBackOnOverleafLoggedIn: an unparseable URL is never treated as logged in, never throws", () => {
  assert.equal(isBackOnOverleafLoggedIn("not a url at all", BASE_URL), false);
  assert.equal(isBackOnOverleafLoggedIn("", BASE_URL), false);
  assert.equal(isBackOnOverleafLoggedIn(null, BASE_URL), false);
});

// --- waitForLogin: Chrome-exit detection (real, live bug, 2026-09-24) -----
//
// Adam: clicking any of Google/ORCID/IEEE on the real Overleaf login page
// made "the chrome tab popup window just flat out exits". The root CAUSE of
// that crash is still unconfirmed (could not be reproduced in an isolated
// test profile against any of the three providers) -- but regardless of
// cause, the OLD waitForLogin swallowed a dead Chrome process identically to
// a harmless mid-navigation polling hiccup, so it would have silently
// retried for the full 5-minute timeout and reported nothing more useful
// than "Timed out waiting for login" even though Chrome had already exited
// seconds in. These tests cover the fix: a real process-exit event now
// rejects immediately with a specific, actionable message.

function fakeClientThatNeverNavigatesAway() {
  return { Page: { getFrameTree: async () => ({ frameTree: { frame: { url: "https://www.overleaf.com/login" } } }) } };
}

test("waitForLogin: resolves once the page navigates away from /login", async () => {
  let call = 0;
  const client = {
    Page: {
      getFrameTree: async () => {
        call += 1;
        const url = call < 2 ? "https://www.overleaf.com/login" : "https://www.overleaf.com/project";
        return { frameTree: { frame: { url } } };
      },
    },
  };
  await assert.doesNotReject(() => waitForLogin(client, "https://www.overleaf.com"));
});

test("waitForLogin: a real Chrome process exit rejects IMMEDIATELY with a specific, actionable message -- not a generic 5-minute timeout", async () => {
  const fakeProcess = new EventEmitter();
  const client = fakeClientThatNeverNavigatesAway();

  const promise = waitForLogin(client, "https://www.overleaf.com", { process: fakeProcess });
  fakeProcess.emit("exit", 1, null); // simulate Chrome dying right after launch, before any poll tick

  await assert.rejects(() => promise, (err: unknown) => {
    assert.match((err as Error).message, /Chrome exited unexpectedly/);
    assert.match((err as Error).message, /code=1/);
    return true;
  });
});

test("waitForLogin: a signal-based exit (e.g. killed) is reported with the signal, not just a code", async () => {
  const fakeProcess = new EventEmitter();
  const client = fakeClientThatNeverNavigatesAway();

  const promise = waitForLogin(client, "https://www.overleaf.com", { process: fakeProcess });
  fakeProcess.emit("exit", null, "SIGKILL");

  await assert.rejects(() => promise, /signal=SIGKILL/);
});

test("waitForLogin: omitting chrome entirely (no third arg) never throws -- old callers/tests keep working", async () => {
  let call = 0;
  const client = {
    Page: {
      getFrameTree: async () => {
        call += 1;
        const url = call < 2 ? "https://www.overleaf.com/login" : "https://www.overleaf.com/project";
        return { frameTree: { frame: { url } } };
      },
    },
  };
  await assert.doesNotReject(() => waitForLogin(client, "https://www.overleaf.com"));
});

test("waitForLogin: THE ACTUAL BUG SCENARIO -- navigating to a third-party OAuth provider must NOT be treated as login success", async () => {
  // Exact sequence a real Google-SSO click-through produces: /login, then
  // the third-party identifier page (this is where the OLD code wrongly
  // resolved), then eventually back on Overleaf's own domain.
  const urls = [
    "https://www.overleaf.com/login",
    "https://accounts.google.com/v3/signin/identifier?client_id=47304055603.apps.googleusercontent.com",
    "https://accounts.google.com/v3/signin/identifier?client_id=47304055603.apps.googleusercontent.com", // human is still typing/thinking
    "https://www.overleaf.com/users/auth/google_oauth2/callback?code=abc",
  ];
  let call = 0;
  const client = {
    Page: {
      getFrameTree: async () => {
        const url = urls[Math.min(call, urls.length - 1)];
        call += 1;
        return { frameTree: { frame: { url } } };
      },
    },
  };
  await assert.doesNotReject(() => waitForLogin(client, "https://www.overleaf.com"));
  assert.ok(call >= 4, `must have polled through the Google domain without resolving early (only got ${call} polls)`);
});

test("waitForLogin: a transient CDP error mid-poll (page mid-navigation) does NOT reject -- keeps polling", async () => {
  let call = 0;
  const client = {
    Page: {
      getFrameTree: async () => {
        call += 1;
        if (call === 1) throw new Error("transient: target navigating"); // first tick: simulated hiccup
        return { frameTree: { frame: { url: "https://www.overleaf.com/project" } } }; // second tick: recovered
      },
    },
  };
  await assert.doesNotReject(() => waitForLogin(client, "https://www.overleaf.com"));
  assert.ok(call >= 2, "must have actually retried past the transient error, not given up");
});

test("importing this module in a context with no real argv[1] (e.g. node -e) never throws", () => {
  const scriptPath = join(__dirname, "overleaf-login.js");
  // import() needs a real URL, not a bare Windows path (a raw "C:\..."
  // path is misparsed as a URL with scheme "c:") -- pathToFileURLString
  // below is the same fix this exact test is regression-testing, applied
  // to the test's own dynamic import of the module under test.
  const scriptUrl = pathToFileURL(scriptPath).href;
  const output = execFileSync(
    process.execPath,
    ["-e", `import(${JSON.stringify(scriptUrl)}).then(() => console.log("ok")).catch((e) => { console.error(e); process.exit(1); })`],
    { encoding: "utf-8" },
  );
  assert.match(output, /ok/);
});
